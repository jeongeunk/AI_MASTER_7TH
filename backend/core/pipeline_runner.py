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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Windows 콘솔 기본 코드페이지(cp949)는 이모지/일부 한글 조합을 못 그려서 print()가
# UnicodeEncodeError로 죽는다 - 백엔드가 로그 한 줄 때문에 크래시 나면 안 되므로,
# stdout/stderr를 UTF-8로 강제하고 인코딩 불가 문자는 죽는 대신 대체 문자로 넘어가게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from langgraph.types import Command
from langgraph.checkpoint.sqlite import SqliteSaver

import pandas as pd

from agents.langgraph_pipeline import build_graph, CHECKPOINT_DB_PATH
from agents.report_agent import aggregate_results, _compute_tag_label
from backend.core.metrics_store import log_run_metrics, log_token_usage, get_token_usage_summary
from backend.core.logging import format_log_for_display


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
    "parsing": {"label": "명세서 파싱", "tool": "parse_excel_to_df / map_columns_by_header / map_columns_with_llm / validate_row_schema", "model": "gpt-4.1-mini (헤더 매핑 폴백)", "level": "step", "plan_step": "1/6"},
    "meta_init": {"label": "Meta Search 준비", "tool": "상태 초기화", "model": None, "level": "plan", "plan_step": "2/6"},
    "meta_exact_check": {"label": "정확 매칭 확인", "tool": "exact_match_meta_db", "model": None, "level": "step", "plan_step": "2/6"},
    "meta_retrieve": {"label": "다중소스 후보 검색", "tool": "retrieve_candidates (vss column + vss glossary + fuzzy)", "model": "text-embedding-3-large", "level": "step", "plan_step": "2/6"},
    "meta_no_match": {"label": "매칭 후보 없음 처리", "tool": "unresolved(no_match) 기록", "model": None, "level": "step", "plan_step": "2/6"},
    "meta_judge": {"label": "AI 매칭 판단", "tool": "generate_match_judgment", "model": "gpt-5-mini", "level": "decision", "plan_step": "2/6"},
    "meta_retry": {"label": "재검색 준비", "tool": "expand_retrieval_params", "model": None, "level": "self_correction", "plan_step": "2/6"},
    "meta_human_confirm": {"label": "담당자 확인 대기", "tool": "request_inferred_confirmation (interrupt)", "model": None, "level": "human", "plan_step": "2/6"},
    "meta_more_candidates": {"label": "후보군조회(담당자 요청)", "tool": "get_more_candidates (floor 없이 순위 기반)", "model": "gpt-5-mini (확장된 후보 재판단)", "level": "self_correction", "plan_step": "2/6"},
    "meta_table_disambiguation": {"label": "테이블 선택 대기", "tool": "request_table_disambiguation (interrupt)", "model": None, "level": "human", "plan_step": "2/6"},
    "join_resolution": {"label": "조인 가능성 검증", "tool": "find_join_path / generate_join_key_candidates / check_value_overlap", "model": None, "level": "step", "plan_step": "3/6"},
    "db_validation": {"label": "실 DB 검증", "tool": "check_column_exists / query_column_type / query_retention_period", "model": None, "level": "step", "plan_step": "4/6"},
    "classification": {"label": "최종 태깅", "tool": "classify_existence / tag_from_type_status / compare_period", "model": None, "level": "step", "plan_step": "5/6"},
    "report": {"label": "명세서 생성", "tool": "aggregate_results / generate_excel_report", "model": "gpt-4.1 (근거 문구 다듬기, 선택적)", "level": "step", "plan_step": "6/6"},
}

PLAN_ANNOUNCEMENT = (
    "Planning: 6-step plan generated "
    "(1 Parsing → 2 Meta Search[exact→retrieve→judge→auto/retry/human] → "
    "3 Join Resolution → 4 DB Validation → 5 Classification → 6 Report). Simulating plan..."
)


def _summarize_update(node_name: str, update: dict) -> str:
    """단순 'Thinking... → Tool Call...' 로그가 아니라, 왜 이 경로로 갔는지가 드러나는
    고수준(plan-level) 로그 문구를 만든다. LangGraph의 conditional_edge 자체가
    사전에 선언된 계획(plan)이므로, 각 노드 로그는 '계획의 몇 번째 단계를 어떤 판단으로
    실행했는지'를 서술한다."""
    if node_name == "parsing":
        n = len(update.get("parsed_rows", []))
        return f"Step 1/6 실행 — 규칙 매칭 우선 시도 → 실패 필드만 LLM 폴백 → 그래도 실패 시 담당자 확인. 결과: {n}건 파싱 완료"
    if node_name == "meta_init":
        return f"Step 2/6 서브플랜 초기화 — {len(update.get('meta_columns', []))}개 컬럼에 대해 exact→retrieve→judge 상태 기계 순회 시작"
    if node_name == "meta_exact_check":
        ef = update.get("exact_found")
        if ef is True:
            return "정확 매칭 성공 → 후보 검색 단계 스킵(비용 절감)"
        if ef == "empty":
            return "처리할 컬럼 없음 → 서브플랜 종료, 다음 단계로"
        if ef == "ambiguous":
            n = len(update.get("exact_candidates", []))
            return f"정확 매칭됐지만 동일 컬럼명이 테이블 {n}개에 존재 → 자동 확정하지 않고 담당자에게 테이블 선택 위임(interrupt)"
        return "정확 매칭 실패 → 의미 기반 후보 검색으로 분기"
    if node_name == "meta_retrieve":
        return f"다중소스 검색(vss_column + vss_glossary + fuzzy) 실행 → 후보 {len(update.get('current_candidates', []))}건 확보"
    if node_name == "meta_judge":
        j = update.get("current_judgment", {})
        return (
            f"Decision — LLM(gpt-5-mini) 근거: \"{j.get('evidence', '')}\" "
            f"→ 확신도 {j.get('confidence')} → 라우팅: {j.get('recommend_action')} "
            f"(추정 매칭은 확신도 무관 항상 담당자 확인 — 0.70~0.92는 재검색 후 확인, 그 외는 즉시 확인)"
        )
    if node_name == "meta_retry":
        return f"Self-Correction — 확신도 부족 → 검색 파라미터 확장(top_k↑, floor↓) 후 재검색 ({update.get('retrieval_attempts')}/2회차)"
    if node_name == "meta_no_match":
        return "검색 후보 없음 → unresolved 처리, DB Validation/Classification 스킵"
    if node_name == "meta_human_confirm":
        return "확신도 애매(0.70~0.92) → 자동 판단을 유보하고 담당자 확인으로 위임(interrupt)"
    if node_name == "meta_more_candidates":
        if update.get("candidates_exhausted"):
            return "Self-Correction — 담당자가 후보군조회 요청 → floor 없이 순위 기반 재검색했으나 더 보여줄 후보 없음(풀 소진)"
        n = len(update.get("current_candidates", []))
        return f"Self-Correction — 담당자가 후보군조회 요청 → floor 없이 순위 기반으로 후보 확장(누적 {n}건) → 재판단 후 다시 확인 요청"
    if node_name == "meta_table_disambiguation":
        m = update.get("meta_results", [])
        last = m[-1] if m else {}
        return f"담당자 선택 반영 — {last.get('match_evidence', '')}"
    if node_name == "join_resolution":
        results = update.get("join_results") or []
        if not results:
            return "Step 3/6 실행 — 요청 컬럼이 단일 테이블뿐이라 조인 검증 스킵"
        joinable = sum(1 for r in results if r.get("status") == "resolved")
        added = [m for m in (update.get("meta_results") or []) if m.get("resolution_path") == "join_key_added"]
        added_note = f" · 요청 목록에 없던 조인키 {len(added)}건을 담당자 승인 후 추가" if added else ""
        return (
            f"Step 3/6 실행 — 기존 관계(경유 포함) 우선 확인 → 없으면 이름/임베딩 후보를 "
            f"실측 값 overlap으로 검증 후 담당자 확인 → 테이블 쌍 {len(results)}건 중 {joinable}건 조인 가능"
            f"{added_note}"
        )
    if node_name == "db_validation":
        return f"Step 4/6 실행 — Guardrail(SELECT-only AST 검증 + READ_ONLY ATTACH) 하 실 DB 대조 → {len(update.get('validation_results', []))}건 검증 완료"
    if node_name == "classification":
        return f"Step 5/6 실행 — 존재→type→기간 순서의 결정론적 규칙으로 최종 태깅 → {len(update.get('classified_results', []))}건 완료"
    if node_name == "report":
        return f"Step 6/6 실행 — 전체 경로별 결과 취합 후 명세서 생성 완료: {update.get('report_excel_path')}"
    return "처리 완료"


# ── 콘솔 로그 (Option B: 파이프라인 실행 중 백엔드 터미널에 실시간 출력) ──────
# 각 Agent의 Tool은 "무슨 기능인지"를 담당자가 아닌 사람도 이해할 수 있도록
# 카테고리 - 쉬운 설명 형태로 표시한다. 같은 함수(예: exact_match_meta_db)를 여러
# Agent가 공유해서 쓰는 경우 문맥이 다르므로, Agent별로 별도 사전을 두고
# agent_label로 먼저 찾은 뒤 없으면 tool 이름만 그대로 보여준다.
_TOOL_DESC_PARSING = {
    "parse_excel_to_df": "제목줄 찾기",
    "read_excel": "제목줄 찾기",
    "rule_scan_header_row": "제목줄 찾기 - 규칙으로 먼저 시도",
    "select_header_row_with_llm": "제목줄 찾기 - 애매하면 AI에게 물어보기",
    "request_header_row_confirmation": "제목줄 찾기 - AI도 모르면 사람에게 직접 물어보기",
    "map_columns_by_header": "컬럼헤더명 확인하기 - 키워드로 바로 알아보기",
    "generate_header_mapping_judgment": "컬럼헤더명 확인하기 - 낯선 이름이면 AI에게 물어보기",
    "header_field_confirmation": "컬럼헤더명 확인하기 - AI도 확신 못 하면 사람에게 직접 물어보기",
    "validate_row_schema": "컬럼명 파악 - 최소한의 정보는 있는지 확인",
    "find_row_completion": "컬럼명 파악 - 실제 데이터베이스에서 부족한 정보 찾아보기",
    "exact_match_meta_db": "컬럼명 파악 - 실제 데이터베이스에서 부족한 정보 찾아보기(정확 매칭)",
    "embed (retrieve_candidates)": "컬럼명 파악 - 실제 데이터베이스에서 부족한 정보 찾아보기(의미 검색)",
    "infer_name_fields_with_llm": "컬럼명 파악 - 사전에도 없으면 AI가 추측하기",
    "request_row_completion_confirmation": "컬럼명 파악 - 채운 값들을 사람에게 한번에 확인하기",
    "map_candidate_tables": "컬럼명 파악 - 컬럼명으로 실제 어느 테이블에 있는지 미리 찾기",
}

_TOOL_DESC_META_SEARCH = {
    "exact_match_meta_db": "동일이름 검색 - 이름이 완전히 똑같은지 찾아보기",
    "request_table_disambiguation": "동일이름 검색 - 같은 이름이 여러 곳에 있으면 사람에게 확인",
    "embed (retrieve_candidates)": "동일이름 없을 경우 - 뜻이 비슷한 컬럼 찾기",
    "vss_search (column_embeddings)": "동일이름 없을 경우 - 뜻이 비슷한 컬럼 찾기(임베딩 유사도)",
    "vss_search (glossary_embeddings)": "동일이름 없을 경우 - 뜻이 비슷한 컬럼 찾기(회사 용어사전 등록 표현인지 검색)",
    "vss_search (confirmed_mapping_embeddings)": "동일이름 없을 경우 - 뜻이 비슷한 컬럼 찾기(과거 담당자 확인 사례 · Episodic Memory)",
    "fuzzy_match_candidates": "동일이름 없을 경우 - 뜻이 비슷한 컬럼 찾기(철자 유사도 · 오탈자/표기차이)",
    "generate_match_judgment": "후보 목록 검색 - AI가 후보 중 하나를 골라보기",
    "decide_route": "후보 목록 검색 - 확신이 애매하면 검색 범위 넓혀 한 번 더 찾기",
    "expand_retrieval_params": "후보 목록 검색 - 확신이 애매하면 검색 범위 넓혀 한 번 더 찾기",
    "request_inferred_confirmation": "후보 목록 검색 - 확신도와 상관없이 사람에게 최종 확인받기",
    "get_more_candidates": "후보 목록 검색 - 사람이 더 보여달라고 하면 문턱 없이 다음 순위 보여주기",
    "apply_confirmation_result": "후보 목록 검색 - 사람의 결정을 최종 결과에 반영하기",
    "update_meta_tag": "확정 결과 기록 - 최종 판정을 DB에 기록하기",
    "persist_confirmed_mapping_example": "확정 결과 기록 - 검증된 매칭을 \"사전\"에 저장해두기",
    "log_confirmation_to_audit": "확정 결과 기록 - 담당자가 무엇을 왜 승인/거절했는지 이력 남기기",
    "get_table_relationships": "확정 결과 기록 - 다른 테이블과 연결될 수 있는지도 같이 확인해두기",
}

_TOOL_DESC_DB_VALIDATION = {
    "get_guarded_connection": "DB 연결 - 읽기 전용으로만 연결하기",
    "validate_query_structure": "DB 연결 - 수정, 삭제 같은 위험한 명령 걸러내기",
    "check_column_exists": "데이터 확인 - 실제로 존재하는지 확인",
    "query_column_type": "데이터 확인 - 데이터 종류 비교하기",
    "compare_type_immediate": "데이터 확인 - 데이터 종류 비교하기",
    "request_type_confirmation": "데이터 확인 - 데이터 종류 다르면 담당자에게 확인받기",
    "detect_month_like_columns": "데이터 시점 확인 - 시점 나타내는 열 찾기",
    "query_retention_period": "데이터 시점 확인 - 보유기간 계산하기",
}

_TOOL_DESC_JOIN_RESOLUTION = {
    "load_table_relationships": "기존 관계 확인 - 기존 연결 관계 불러오기",
    "find_join_path": "기존 관계 확인 - 기존 연결 관계 불러오기",
    "find_shared_identifier_columns": "기존 관계 없는 경우, 조인 후보 검색 - 이름이 같은 열 찾기",
    "find_semantic_key_candidates": "기존 관계 없는 경우, 조인 후보 검색 - 뜻이 비슷한 열 찾기",
    "check_value_overlap": "조인 후보 확인 - 실제 값이 겹치는지 세어보기",
    "request_join_key_confirmation": "조인 후보 확인 - 담당자에게 최종 확인받기",
    "persist_confirmed_relationship": "조인 후보 확인 - 확정된 연결 저장해두기",
    "validate_join_path": "조인 후보 실 검증 - 종류·기간 맞는지 점검",
    "find_missing_join_key_columns": "조인 후보 목록 추가",
    "request_missing_key_confirmation": "조인 후보 목록 추가",
}

_TOOL_DESC_CLASSIFICATION = {
    "classify_existence": "존재 여부 판정",
    "tag_from_type_status": "종류(타입) 불일치 반영",
    "compare_period": "기간 비교하기",
    "intersect_periods": "기간 비교하기",
}

_TOOL_DESC_REPORT = {
    "aggregate_results": "결과 취합하기",
    "build_join_report": "조인 결과 따로 모으기",
    "generate_excel_report": "엑셀로 만들기",
    "compute_summary_stats": "요약 통계 내기",
    "log_revision_snapshot": "변경 이력 남기기",
}

# agent_label(instrument_agent에 준 라벨)의 접두어로 어느 사전을 쓸지 고른다.
# 예: "Meta Search Agent (retrieve)"도 "Meta Search Agent"로 시작하므로 매칭된다.
_TOOL_DESC_BY_AGENT = {
    "Parsing Agent": _TOOL_DESC_PARSING,
    "Meta Search Agent": _TOOL_DESC_META_SEARCH,
    "DB Validation Agent": _TOOL_DESC_DB_VALIDATION,
    "Join Resolution Agent": _TOOL_DESC_JOIN_RESOLUTION,
    "Classification Agent": _TOOL_DESC_CLASSIFICATION,
    "Report Agent": _TOOL_DESC_REPORT,
}


def _tool_desc(tool_name: str, agent_label: str = None) -> str:
    if agent_label:
        for prefix, table in _TOOL_DESC_BY_AGENT.items():
            if not agent_label.startswith(prefix):
                continue
            for key, desc in table.items():
                if tool_name.startswith(key):
                    return desc
            break  # agent는 찾았는데 그 사전엔 없으면, 다른 Agent 사전은 뒤지지 않는다
    # agent_label이 없거나 매핑이 안 되면(과거 호출부 호환) 전체 사전에서 찾아본다
    for table in _TOOL_DESC_BY_AGENT.values():
        for key, desc in table.items():
            if tool_name.startswith(key):
                return desc
    return tool_name


def _print_console_log(thread_id: str, node_name: str, agent_label: str, trace: dict | None, summary: str) -> None:
    """실측 tool_calls(모델/입력/결정 포함)를 콘솔에 그대로 흘려보낸다.
    trace가 없으면(instrument_agent로 계측 안 된 노드) 노드 완료 한 줄만 찍는다.
    thread_id 앞 8자리를 매 줄 앞에 붙인다 - 동시에 여러 파이프라인이 돌면 콘솔에 로그가
    뒤섞이는데, 이 태그가 없으면 어느 줄이 어느 실행에서 나온 건지 구분할 방법이 없었다."""
    tid = thread_id[:8]
    tool_calls = trace.get("tool_calls", []) if trace else []
    if not tool_calls:
        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{now}] [{tid}] 📍 [{agent_label}] {summary}")
        return

    for tc in tool_calls:
        ts = datetime.fromtimestamp(tc["start"]).strftime("%H:%M:%S.%f")[:-3]
        # 진짜 담당자 확인(HITL) 함수는 전부 이름에 명시적으로 "(HITL)"을 붙이는 관례를
        # 따른다(request_table_disambiguation (HITL) 등). "confirmation" 부분 매칭이나
        # request_ 접두어로 판정하면 log_confirmation_to_audit(기록만 함)나
        # apply_confirmation_result(변환만 함)처럼 사람에게 아무것도 안 묻는 함수까지
        # 🙋로 오인되므로 쓰지 않는다.
        is_hitl = "HITL" in tc["tool"]
        marker = "🙋" if is_hitl else "🔧"
        ctx_suffix = f" ({tc['context']})" if tc.get("context") else ""
        print(f"[{ts}] [{tid}] 📍 [{agent_label}] → [{tc['tool']} : {_tool_desc(tc['tool'], agent_label)}]{ctx_suffix}")
        print(f"   {marker} Tool: {tc['tool']}  ({tc['duration_sec']}s{'  ok' if tc['ok'] else '  FAILED'})")
        if tc.get("model"):
            print(f"   🤖 Model: {tc['model']}")
        if tc.get("args") is not None:
            print(f"   📝 Args: {tc['args']}")
        if tc.get("result") is not None:
            print(f"   💬 {tc['result']}")
        if not tc.get("ok") and tc.get("error"):
            print(f"   ❌ Error: {tc['error']}")

    hitl_count = sum(1 for tc in tool_calls if "HITL" in tc["tool"] or "confirmation" in tc["tool"])
    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{now}] [{tid}] 📝 [{agent_label}] 완료 — {summary}"
          f"{f' (담당자 확인 {hitl_count}회)' if hitl_count else ''}")


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
        self.started_at = self._last_event_time

    def push_event(self, node_name: str, update: dict):
        meta = NODE_METADATA.get(node_name, {"label": node_name, "tool": node_name, "model": None, "level": "step", "plan_step": None})
        update = dict(update or {})
        trace = update.pop("trace_log", None)
        agent_logs = update.pop("agent_logs", None) or []
        now = time.time()
        elapsed = round(now - self._last_event_time, 2)
        self._last_event_time = now

        tool_calls = trace.get("tool_calls", []) if trace else []
        # NODE_METADATA["model"]은 "이 노드가 LLM을 쓸 수 있다"는 고정 안내문일 뿐, 이번 실행에서
        # 실제로 호출됐는지는 알려주지 않는다(예: 규칙만으로 다 해결되면 LLM을 전혀 안 씀).
        # tool_span이 실제로 기록한 tool_calls의 model 필드가 있으면 그걸 실측값으로 우선 사용한다.
        actual_models = sorted({tc["model"] for tc in tool_calls if tc.get("model")})
        if actual_models:
            model_label = ", ".join(actual_models)
        elif trace is not None:
            # trace_log는 있지만(=instrument_agent로 계측된 노드) 실제 모델 호출은 0건
            model_label = "(모델 호출 없음 - 규칙만으로 해결됨)"
        else:
            # 애초에 계측 안 된 노드(트레이스 자체가 없음) - 기존 고정 라벨로 폴백
            model_label = meta["model"]

        summary = _summarize_update(node_name, update)
        with self.lock:
            self.events.append({
                "seq": len(self.events),
                "time": now,
                "node": node_name,
                "label": meta["label"],
                "tool": meta["tool"],
                "model": model_label,
                "level": meta.get("level", "step"),
                "plan_step": meta.get("plan_step"),
                "elapsed_sec": elapsed,
                "summary": summary,
                "agent_start": trace.get("agent_start") if trace else None,
                "agent_end": trace.get("agent_end") if trace else None,
                "agent_duration_sec": trace.get("agent_duration_sec") if trace else None,
                "tool_calls": tool_calls,
                "agent_logs": agent_logs,  # make_log() 고수준 로그(7대 구성요소 분류) - 트레이스 시각화 페이지용
            })

        agent_label = (trace or {}).get("agent") or meta["label"]
        _print_console_log(self.thread_id, node_name, agent_label, trace, summary)

        # make_log() 고수준 로그 콘솔 출력 - 이모지는 장식일 뿐이고 실제 정보는 항상
        # "[구성요소] 요약" 대괄호 텍스트로 담는다(기존 tool_span 콘솔 로그와 같은 원칙).
        # 콘솔 폰트가 이모지를 못 그려도 대괄호 텍스트는 항상 읽힌다.
        for log in agent_logs:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{ts}] [{self.thread_id[:8]}] {format_log_for_display(log)}")

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


def _run_stream(run: PipelineRun, graph_app, config: dict):
    """
    [2차 개정] 1차 개정(재귀 제거 + 명시적 gen.close())으로도 LangSmith 트레이스에는
    여전히 GeneratorExit가 "비정상 종료"로 기록됐다. 진짜 원인은 close() 자체가
    아니라 - interrupt 청크를 받자마자 break로 제너레이터를 "덜 쓴 채로" 버렸다는 것.

    LangGraph의 stream()은 interrupt()가 걸리면 그 interrupt 청크를 마지막으로
    스스로 자연 종료된다(재개 입력을 아직 못 받았으니 더 계산할 게 없어서 다음
    next() 호출에서 StopIteration을 정상적으로 냄). break로 먼저 끊지 않고 그냥
    한 바퀴 더 돌게(continue) 두면 제너레이터가 스스로 곱게 끝나서 close()도
    필요 없고, GeneratorExit 자체가 아예 발생하지 않는다 - 이제 LangSmith도
    이 구간을 정상 종료로 기록한다.
    """
    resume_command = None
    try:
        while True:
            stream_input = resume_command if resume_command is not None else {"input_file": run.input_file}
            interrupted = False
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
                    resume_command = Command(resume=decision)
                    interrupted = True
                    continue  # break 아님 - 제너레이터가 다음 next()에서 스스로 StopIteration 내며 종료되게 둠
                for node_name, update in chunk.items():
                    run.push_event(node_name, update)
            # for문이 여기까지 왔다는 건 제너레이터가 StopIteration으로 자연 종료됐다는 뜻
            # (close() 불필요, GeneratorExit 발생 안 함)

            if interrupted:
                continue  # while 다음 회차에서 재개된 stream() 새로 시작

            final_state = graph_app.get_state(config).values
            run.report = {
                "excel_path": final_state.get("report_excel_path"),
                "stats": final_state.get("report_stats"),
            }
            run_completed_at = time.time()
            log_run_metrics(
                run.thread_id, run.input_file, final_state.get("meta_results", []),
                run.started_at, run_completed_at,
                report_rows=final_state.get("report_rows", []),
            )

            # 토큰 사용량 집계 - Report Agent까지 끝난 이 시점에는 run.events에 이번
            # 실행에서 발생한 tool_call 전체가 이미 쌓여 있다(tool_span이 기록한 것).
            # tokens 필드가 있는 것만(=LLM/임베딩 호출) 골라 감사 DB에 적재하고 콘솔에 남긴다.
            token_calls = [
                tc for ev in run.events for tc in (ev.get("tool_calls") or [])
                if tc.get("tokens")
            ]
            log_token_usage(run.thread_id, run.input_file, token_calls)
            usage_summary = get_token_usage_summary(run.thread_id)
            if usage_summary["total"]:
                breakdown = " · ".join(f"{m} {t:,}" for m, t in usage_summary["by_model"].items())
                now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"[{now}] [{run.thread_id[:8]}] 📊 [Token Usage] "
                      f"총 {usage_summary['total']:,} 토큰 사용 ({breakdown})")

            run.status = "done"
            return
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