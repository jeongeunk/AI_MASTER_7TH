"""
backend/core/pipeline_runner.py

FastAPI가 LangGraph 파이프라인을 백그라운드 스레드에서 실행하고,
노드가 완료될 때마다 이벤트(어떤 tool/LLM이 쓰였는지 포함)를 메모리에 쌓아
프론트엔드(Streamlit)가 폴링으로 조회할 수 있게 하는 모듈.

interrupt()가 발생하면 스레드는 threading.Event로 대기하고,
/confirm 엔드포인트가 결정을 넣어주면 Command(resume=...)로 이어서 실행한다.
(agents/langgraph_pipeline.py의 run_pipeline.py 콘솔 루프를 스레드+API 버전으로 옮긴 것)
"""

import os
import sys
import time
import uuid
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from langgraph.types import Command
from langgraph.checkpoint.sqlite import SqliteSaver

import pandas as pd

from agents.langgraph_pipeline import build_graph, CHECKPOINT_DB_PATH
from agents.report_agent import aggregate_results, _compute_tag_label


def _json_safe(value):
    """pandas 3.0 기본 str dtype은 결측값을 NaN으로 저장하므로,
    JSON 응답(allow_nan=False) 직전에 NaN/NaT를 None으로 되돌린다."""
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value

# ── 노드별 tool/LLM 메타데이터 (모니터링 화면 표시용) ─────────────
# level: "plan"(전체 계획 선언) | "decision"(분기 판단) | "self_correction"(재시도/보정)
#        | "human"(담당자 개입) | "step"(일반 실행 단계)
NODE_METADATA = {
    "parsing": {"label": "명세서 파싱", "tool": "parse_excel_to_df / map_columns_by_header / map_columns_with_llm / validate_row_schema", "model": "gpt-4.1-mini (헤더 매핑 폴백)", "level": "step", "plan_step": "1/5"},
    "meta_init": {"label": "Meta Search 준비", "tool": "상태 초기화", "model": None, "level": "plan", "plan_step": "2/5"},
    "meta_exact_check": {"label": "정확 매칭 확인", "tool": "exact_match_meta_db", "model": None, "level": "step", "plan_step": "2/5"},
    "meta_retrieve": {"label": "다중소스 후보 검색", "tool": "retrieve_candidates (vss column + vss glossary + fuzzy)", "model": "text-embedding-3-large", "level": "step", "plan_step": "2/5"},
    "meta_no_match": {"label": "매칭 후보 없음 처리", "tool": "unresolved(no_match) 기록", "model": None, "level": "step", "plan_step": "2/5"},
    "meta_judge": {"label": "AI 매칭 판단", "tool": "generate_match_judgment", "model": "gpt-5-mini", "level": "decision", "plan_step": "2/5"},
    "meta_auto_confirm": {"label": "자동 확정", "tool": "update_meta_tag / log_auto_confirm", "model": None, "level": "step", "plan_step": "2/5"},
    "meta_retry": {"label": "재검색 준비", "tool": "expand_retrieval_params", "model": None, "level": "self_correction", "plan_step": "2/5"},
    "meta_human_confirm": {"label": "담당자 확인 대기", "tool": "request_inferred_confirmation (interrupt)", "model": None, "level": "human", "plan_step": "2/5"},
    "db_validation": {"label": "실 DB 검증", "tool": "check_column_exists / query_column_type / query_retention_period", "model": None, "level": "step", "plan_step": "3/5"},
    "classification": {"label": "최종 태깅", "tool": "classify_existence / tag_from_type_status / compare_period", "model": None, "level": "step", "plan_step": "4/5"},
    "report": {"label": "명세서 생성", "tool": "aggregate_results / generate_excel_report", "model": "gpt-4.1 (근거 문구 다듬기, 선택적)", "level": "step", "plan_step": "5/5"},
}

PLAN_ANNOUNCEMENT = (
    "Planning: 5-step plan generated "
    "(1 Parsing → 2 Meta Search[exact→retrieve→judge→auto/retry/human] → "
    "3 DB Validation → 4 Classification → 5 Report). Simulating plan..."
)


def _summarize_update(node_name: str, update: dict) -> str:
    """단순 'Thinking... → Tool Call...' 로그가 아니라, 왜 이 경로로 갔는지가 드러나는
    고수준(plan-level) 로그 문구를 만든다. LangGraph의 conditional_edge 자체가
    사전에 선언된 계획(plan)이므로, 각 노드 로그는 '계획의 몇 번째 단계를 어떤 판단으로
    실행했는지'를 서술한다."""
    if node_name == "parsing":
        n = len(update.get("parsed_rows", []))
        return f"Step 1/5 실행 — 규칙 매칭 우선 시도 → 실패 필드만 LLM 폴백 → 그래도 실패 시 담당자 확인. 결과: {n}건 파싱 완료"
    if node_name == "meta_init":
        return f"Step 2/5 서브플랜 초기화 — {len(update.get('meta_columns', []))}개 컬럼에 대해 exact→retrieve→judge 상태 기계 순회 시작"
    if node_name == "meta_exact_check":
        ef = update.get("exact_found")
        if ef is True:
            return "정확 매칭 성공 → 후보 검색 단계 스킵(비용 절감)"
        if ef == "empty":
            return "처리할 컬럼 없음 → 서브플랜 종료, 다음 단계로"
        return "정확 매칭 실패 → 의미 기반 후보 검색으로 분기"
    if node_name == "meta_retrieve":
        return f"다중소스 검색(vss_column + vss_glossary + fuzzy) 실행 → 후보 {len(update.get('current_candidates', []))}건 확보"
    if node_name == "meta_judge":
        j = update.get("current_judgment", {})
        return (
            f"Decision — LLM(gpt-5-mini) 근거: \"{j.get('evidence', '')}\" "
            f"→ 확신도 {j.get('confidence')} → 라우팅: {j.get('recommend_action')} "
            f"(≥0.92 auto_confirm / <0.70 human_confirm / 그 사이는 retry)"
        )
    if node_name == "meta_auto_confirm":
        return "확신도 임계값(0.92) 이상 → 담당자 확인 없이 자동 확정"
    if node_name == "meta_retry":
        return f"Self-Correction — 확신도 부족 → 검색 파라미터 확장(top_k↑, floor↓) 후 재검색 ({update.get('retrieval_attempts')}/2회차)"
    if node_name == "meta_no_match":
        return "검색 후보 없음 → unresolved 처리, DB Validation/Classification 스킵"
    if node_name == "meta_human_confirm":
        return "확신도 애매(0.70~0.92) → 자동 판단을 유보하고 담당자 확인으로 위임(interrupt)"
    if node_name == "db_validation":
        return f"Step 3/5 실행 — Guardrail(SELECT-only AST 검증 + READ_ONLY ATTACH) 하 실 DB 대조 → {len(update.get('validation_results', []))}건 검증 완료"
    if node_name == "classification":
        return f"Step 4/5 실행 — 존재→type→기간 순서의 결정론적 규칙으로 최종 태깅 → {len(update.get('classified_results', []))}건 완료"
    if node_name == "report":
        return f"Step 5/5 실행 — 전체 경로별 결과 취합 후 명세서 생성 완료: {update.get('report_excel_path')}"
    return "처리 완료"


class PipelineRun:
    def __init__(self, thread_id: str, input_file: str):
        self.thread_id = thread_id
        self.input_file = input_file
        self.events = []
        self.lock = threading.Lock()
        self.status = "running"  # running | waiting_human | done | error
        self.confirm_payload = None
        self.resume_value = None
        self.resume_event = threading.Event()
        self.report = None
        self.error = None
        self._last_event_time = time.time()

    def push_event(self, node_name: str, update: dict):
        meta = NODE_METADATA.get(node_name, {"label": node_name, "tool": node_name, "model": None, "level": "step", "plan_step": None})
        update = dict(update or {})
        trace = update.pop("trace_log", None)
        now = time.time()
        elapsed = round(now - self._last_event_time, 2)
        self._last_event_time = now
        with self.lock:
            self.events.append({
                "seq": len(self.events),
                "time": now,
                "node": node_name,
                "label": meta["label"],
                "tool": meta["tool"],
                "model": meta["model"],
                "level": meta.get("level", "step"),
                "plan_step": meta.get("plan_step"),
                "elapsed_sec": elapsed,
                "summary": _summarize_update(node_name, update),
                "agent_start": trace.get("agent_start") if trace else None,
                "agent_end": trace.get("agent_end") if trace else None,
                "agent_duration_sec": trace.get("agent_duration_sec") if trace else None,
                "tool_calls": trace.get("tool_calls", []) if trace else [],
            })

    def push_plan_announcement(self):
        now = time.time()
        self._last_event_time = now
        with self.lock:
            self.events.append({
                "seq": len(self.events),
                "time": now,
                "node": "_plan",
                "label": "Planning",
                "tool": None,
                "model": None,
                "level": "plan",
                "plan_step": None,
                "elapsed_sec": 0.0,
                "summary": PLAN_ANNOUNCEMENT,
                "agent_start": now,
                "agent_end": now,
                "agent_duration_sec": 0.0,
                "tool_calls": [],
            })

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            new_events = self.events[since:]
        return {
            "events": new_events,
            "next_since": len(self.events),
            "status": self.status,
            "confirm_payload": self.confirm_payload,
            "report": self.report,
            "error": self.error,
        }


RUNS: dict[str, PipelineRun] = {}
_RUNS_LOCK = threading.Lock()


def _run_stream(run: PipelineRun, graph_app, config: dict, resume_command=None):
    try:
        stream_input = resume_command if resume_command is not None else {"input_file": run.input_file}
        for chunk in graph_app.stream(stream_input, config=config, stream_mode="updates"):
            if "__interrupt__" in chunk:
                interrupt_obj = chunk["__interrupt__"][0]
                run.confirm_payload = interrupt_obj.value
                run.status = "waiting_human"
                run.resume_event.clear()
                run.resume_event.wait()  # /confirm 엔드포인트가 set() 할 때까지 대기
                decision = run.resume_value
                run.status = "running"
                run.confirm_payload = None
                _run_stream(run, graph_app, config, resume_command=Command(resume=decision))
                return
            for node_name, update in chunk.items():
                run.push_event(node_name, update)

        final_state = graph_app.get_state(config).values
        run.report = {
            "excel_path": final_state.get("report_excel_path"),
            "stats": final_state.get("report_stats"),
        }
        run.status = "done"
    except Exception as e:  # noqa: BLE001 - 백그라운드 스레드 예외를 상태로 노출
        run.status = "error"
        run.error = str(e)


def _thread_main(run: PipelineRun):
    run.push_plan_announcement()
    graph = build_graph()
    with SqliteSaver.from_conn_string(CHECKPOINT_DB_PATH) as checkpointer:
        graph_app = graph.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": run.thread_id}}
        _run_stream(run, graph_app, config)


def start_pipeline(input_file: str) -> str:
    thread_id = str(uuid.uuid4())
    run = PipelineRun(thread_id, input_file)
    with _RUNS_LOCK:
        RUNS[thread_id] = run

    t = threading.Thread(target=_thread_main, args=(run,), daemon=True)
    t.start()
    return thread_id


def get_events_since(thread_id: str, since: int = 0) -> dict:
    run = RUNS.get(thread_id)
    if not run:
        raise KeyError(f"알 수 없는 thread_id: {thread_id}")
    return run.snapshot(since)


def submit_confirmation(thread_id: str, decision: str):
    run = RUNS.get(thread_id)
    if not run:
        raise KeyError(f"알 수 없는 thread_id: {thread_id}")
    if run.status != "waiting_human":
        raise ValueError("현재 담당자 확인 대기 상태가 아닙니다")
    run.resume_value = decision
    run.resume_event.set()


def get_results(thread_id: str) -> dict:
    run = RUNS.get(thread_id)
    if not run:
        raise KeyError(f"알 수 없는 thread_id: {thread_id}")

    graph = build_graph()
    with SqliteSaver.from_conn_string(CHECKPOINT_DB_PATH) as checkpointer:
        graph_app = graph.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        state = graph_app.get_state(config).values

    meta_results = state.get("meta_results", [])
    classified_results = state.get("classified_results", [])
    if not classified_results and not meta_results:
        return {"ready": False, "rows": [], "stats": None}

    merged_df = aggregate_results(meta_results, classified_results)
    if merged_df.empty:
        return {"ready": False, "rows": [], "stats": None}

    merged_df["태그설명"] = merged_df.apply(_compute_tag_label, axis=1)
    rows = [{k: _json_safe(v) for k, v in row.items()} for row in merged_df.to_dict(orient="records")]
    return {"ready": True, "rows": rows, "stats": run.report["stats"] if run.report else None}


def get_download_path(thread_id: str) -> str:
    run = RUNS.get(thread_id)
    if not run or not run.report or not run.report.get("excel_path"):
        raise FileNotFoundError("리포트가 아직 준비되지 않았습니다")
    return run.report["excel_path"]
