"""
SchemaScout LangGraph 파이프라인 (conditional_edge 전환판)

5개 Agent(Parsing -> Meta Search -> DB Validation -> Classification -> Report)를
하나의 StateGraph로 연결한다. Meta Search 단계는 컬럼 단위로 아래 노드를 돌며
add_conditional_edges로 재검색/담당자확인 갈래를 그래프 레벨에서 분기한다.
추정 매칭(정확 매칭이 아닌 경우)은 confidence 크기와 무관하게 항상 담당자 확인을
거친다 - 자동 확정 경로는 없다(고위험 결정이라 LLM 자기 확신도만으로 통과시키지 않음).

    meta_exact_check -> (단일 매칭: 다음 컬럼 또는 join_resolution / 동일 컬럼명이 여러
                          테이블에 존재(ambiguous): meta_table_disambiguation / 매칭 없음: meta_retrieve)
    meta_retrieve -> (후보 없음: meta_no_match / 있음: meta_judge)
    meta_judge -> (retry / human_confirm)
    meta_retry -> meta_retrieve (재검색 루프, 최대 MAX_RETRIEVAL_ATTEMPTS회)
    meta_human_confirm / meta_no_match / meta_table_disambiguation
        -> (다음 컬럼 또는 join_resolution)

담당자 확인(interrupt/inferred)과 type 불일치 확인(interrupt/db_validation)은 기존과
동일하게 노드 함수 내부에서 interrupt()를 호출하는 confirm_fn으로 처리한다.

체크포인터로 SqliteSaver를 사용해 interrupt 대기 상태가 프로세스 재시작 후에도 유지됨.
"""

import os
import sys
from typing import TypedDict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import duckdb
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.checkpoint.sqlite import SqliteSaver

from agents.parsing_agent import run_parsing
from agents.meta_search_agent import (
    META_DB_PATH,
    exact_match_meta_db,
    retrieve_candidates,
    generate_match_judgment,
    decide_route,
    expand_retrieval_params,
    request_inferred_confirmation,
    request_table_disambiguation,
    apply_confirmation_result,
    update_meta_tag,
    log_confirmation_to_audit,
    get_table_relationships,
    persist_confirmed_mapping_example,
    SIMILARITY_PREFILTER_FLOOR,
)
from agents.join_resolution_agent import run_join_resolution
from agents.db_validation_agent import run_db_validation
from agents.classification_agent import run_classification
from agents.report_agent import run_report
from agents.trace import instrument_agent
from llm_client import embed, chat_parsed

load_dotenv()

CHECKPOINT_DB_PATH = os.environ.get("CHECKPOINT_DB_PATH", "./db/langgraph_checkpoints.sqlite")
os.makedirs(os.path.dirname(CHECKPOINT_DB_PATH), exist_ok=True)


# ── State 정의 ───────────────────────────────────────────────
class PipelineState(TypedDict, total=False):
    input_file: str
    parsed_rows: list

    # Meta Search 서브플로우 상태 (컬럼 단위 루프)
    meta_columns: list
    meta_index: int
    meta_results: list
    exact_found: object  # True / False / "empty" / "ambiguous"
    exact_candidates: list  # 동일 컬럼명이 여러 테이블에 있을 때(ambiguous)의 후보 목록
    current_candidates: list
    current_judgment: dict
    retrieval_attempts: int
    current_top_k: int
    current_floor: float
    current_glossary_boost: bool

    join_results: list  # Join Resolution Agent: 여러 테이블을 요청한 명세서의 조인 가능성 검증 결과

    validation_results: list
    classified_results: list
    report_excel_path: Optional[str]
    report_stats: Optional[dict]

    trace_log: Optional[dict]  # 노드 실행의 tool-call 트레이스(pipeline_runner가 읽어 모니터링에 표시)


# ── interrupt() 기반 confirm_fn ────────────────────────────
def graph_confirm_fn(payload: dict) -> str:
    decision = interrupt(payload)
    return decision


def _get_meta_con():
    con = duckdb.connect(META_DB_PATH)
    con.execute("LOAD vss;")
    return con


def _enrich(con, result: dict) -> dict:
    if result.get("meta_row"):
        result["table_relationships"] = get_table_relationships(con, result["meta_row"]["table_id"])
    else:
        result["table_relationships"] = []
    return result


# ── 노드 정의: Parsing ───────────────────────────────────────
@instrument_agent("Parsing Agent")
def parsing_node(state: PipelineState) -> dict:
    result = run_parsing(state["input_file"], confirm_fn=graph_confirm_fn)
    return {"parsed_rows": result["parsed_rows"]}


@instrument_agent("Meta Search Agent (서브플랜 초기화)")
def meta_init_node(state: PipelineState) -> dict:
    return {
        "meta_columns": state["parsed_rows"],
        "meta_index": 0,
        "meta_results": [],
    }


# ── 노드 정의: Meta Search 서브플로우 ────────────────────────
@instrument_agent("Meta Search Agent (exact check)")
def meta_exact_check_node(state: PipelineState) -> dict:
    columns = state["meta_columns"]
    idx = state["meta_index"]
    if idx >= len(columns):
        return {"exact_found": "empty"}

    col = columns[idx]
    con = _get_meta_con()
    exact = exact_match_meta_db(con, str(col["영문명"]).strip())
    if exact["found"]:
        out = {**col, "match_status": "matched", "meta_row": exact["meta_row"],
               "match_evidence": "영문명 정확 매칭", "resolution_path": "validated"}
        out = _enrich(con, out)
        persist_confirmed_mapping_example(con, out, embed)
        con.close()
        return {
            "meta_results": state["meta_results"] + [out],
            "meta_index": idx + 1,
            "exact_found": True,
        }
    if exact.get("ambiguous"):
        con.close()
        return {"exact_found": "ambiguous", "exact_candidates": exact["candidates"]}
    con.close()
    return {
        "exact_found": False,
        "retrieval_attempts": 0,
        "current_top_k": 5,
        "current_floor": SIMILARITY_PREFILTER_FLOOR,
        "current_glossary_boost": False,
    }


def route_after_exact(state: PipelineState) -> str:
    ef = state.get("exact_found")
    if ef == "empty":
        return "done"
    if ef is True:
        return "done" if state["meta_index"] >= len(state["meta_columns"]) else "next_column"
    if ef == "ambiguous":
        return "disambiguate"
    return "search"


@instrument_agent("Meta Search Agent (table disambiguation)")
def meta_table_disambiguation_node(state: PipelineState) -> dict:
    col = state["meta_columns"][state["meta_index"]]
    candidates = state["exact_candidates"]

    decision = request_table_disambiguation(col, candidates, confirm_fn=graph_confirm_fn)
    table_ids = [c["table_id"] for c in candidates]

    con = _get_meta_con()
    if decision.get("decision") == "approved" and decision.get("selected_table_id"):
        selected = next(c for c in candidates if c["table_id"] == decision["selected_table_id"])
        out = {**col, "match_status": "matched", "meta_row": selected,
               "match_evidence": f"영문명 정확 매칭 - 동일 컬럼명이 {len(table_ids)}개 테이블"
                                  f"({', '.join(table_ids)})에 존재해 담당자가 {selected['table_id']} 선택",
               "resolution_path": "validated"}
        out = _enrich(con, out)
        persist_confirmed_mapping_example(con, out, embed)
    else:
        out = {**col, "match_status": "unresolved", "meta_row": None,
               "match_evidence": f"동일 컬럼명이 {len(table_ids)}개 테이블({', '.join(table_ids)})에 존재하나 "
                                  f"담당자가 테이블을 선택하지 않음",
               "unresolved_reason": "ambiguous_table_rejected", "resolution_path": "rejected_by_human"}
    con.close()

    return {
        "meta_results": state["meta_results"] + [out],
        "meta_index": state["meta_index"] + 1,
    }


@instrument_agent("Meta Search Agent (retrieve)")
def meta_retrieve_node(state: PipelineState) -> dict:
    col = state["meta_columns"][state["meta_index"]]
    con = _get_meta_con()
    candidates = retrieve_candidates(
        con, str(col["영문명"]).strip(), str(col["항목설명"]), embed,
        top_k=state["current_top_k"], floor=state["current_floor"],
        include_glossary_boost=state["current_glossary_boost"],
    )
    con.close()
    return {"current_candidates": candidates}


def route_after_retrieve(state: PipelineState) -> str:
    return "judge" if state["current_candidates"] else "no_candidates"


@instrument_agent("Meta Search Agent (no match)")
def meta_no_match_node(state: PipelineState) -> dict:
    col = state["meta_columns"][state["meta_index"]]
    out = {**col, "match_status": "unresolved", "meta_row": None,
           "match_evidence": "검색된 후보 없음", "unresolved_reason": "no_match",
           "resolution_path": "no_match", "retrieval_attempts": state["retrieval_attempts"]}
    return {
        "meta_results": state["meta_results"] + [out],
        "meta_index": state["meta_index"] + 1,
    }


@instrument_agent("Meta Search Agent (LLM judge)")
def meta_judge_node(state: PipelineState) -> dict:
    col = state["meta_columns"][state["meta_index"]]
    judgment = generate_match_judgment(col, state["current_candidates"], chat_fn=chat_parsed)
    return {"current_judgment": judgment}


def route_by_judgment(state: PipelineState) -> str:
    return decide_route(state["current_judgment"], state["retrieval_attempts"])


@instrument_agent("Meta Search Agent (self-correction retry)")
def meta_retry_node(state: PipelineState) -> dict:
    attempts = state["retrieval_attempts"] + 1
    params = expand_retrieval_params(attempts)
    return {
        "retrieval_attempts": attempts,
        "current_top_k": params["top_k"],
        "current_floor": params["floor"],
        "current_glossary_boost": params["include_glossary_boost"],
    }


@instrument_agent("Meta Search Agent (human confirm)")
def meta_human_confirm_node(state: PipelineState) -> dict:
    col = state["meta_columns"][state["meta_index"]]
    judgment = state["current_judgment"]
    candidates = state["current_candidates"]

    decision = request_inferred_confirmation(col, candidates, judgment, confirm_fn=graph_confirm_fn)
    apply_confirmation_result(decision)  # 반환값은 아래에서 재구성하므로 로깅 목적만

    target_column_id = judgment.get("selected_column_id") or (candidates[0]["column_id"] if candidates else None)
    if target_column_id:
        log_confirmation_to_audit(
            target_column_id,
            question=f"{col['영문명']} -> LLM 추천(확신도 {judgment['confidence']}) 승인?",
            answer=decision,
        )

    con = _get_meta_con()
    if decision == "approved" and target_column_id:
        selected = next((c for c in candidates if c["column_id"] == target_column_id), candidates[0])
        update_meta_tag(con, selected["column_id"], "inferred_confirmed", confidence=judgment["confidence"])
        out = {**col, "match_status": "inferred_confirmed", "meta_row": selected["meta_row"],
               "match_evidence": judgment["evidence"], "llm_confidence": judgment["confidence"],
               "llm_evidence": judgment["evidence"], "resolution_path": "validated",
               "retrieval_attempts": state["retrieval_attempts"]}
        out = _enrich(con, out)
        persist_confirmed_mapping_example(con, out, embed)
    else:
        if target_column_id:
            update_meta_tag(con, target_column_id, "unresolved", confidence=judgment["confidence"])
        out = {**col, "match_status": "unresolved", "meta_row": None,
               "match_evidence": "담당자 확인 결과 거절", "unresolved_reason": "rejected_by_human",
               "resolution_path": "rejected_by_human", "retrieval_attempts": state["retrieval_attempts"]}
    con.close()

    return {
        "meta_results": state["meta_results"] + [out],
        "meta_index": state["meta_index"] + 1,
    }


def route_after_resolution(state: PipelineState) -> str:
    return "done" if state["meta_index"] >= len(state["meta_columns"]) else "next_column"


# ── 노드 정의: Join Resolution (Meta Search 서브플로우 완료 직후) ─
@instrument_agent("Join Resolution Agent")
def join_resolution_node(state: PipelineState) -> dict:
    result = run_join_resolution(state["meta_results"], confirm_fn=graph_confirm_fn)
    # meta_results를 갱신본으로 교체 - 조인에 필요한데 요청 목록에 없던 키 컬럼이
    # 담당자 승인으로 추가됐다면, 이후 DB Validation부터는 이 갱신본을 이어받아야
    # 추가된 키도 다른 요청 컬럼과 똑같이 검증되고 리포트에 노출된다.
    return {"join_results": result["join_results"], "meta_results": result["meta_results"]}


# ── 노드 정의: 이후 단계 (기존과 동일) ─────────────────────────
@instrument_agent("DB Validation Agent")
def db_validation_node(state: PipelineState) -> dict:
    results = run_db_validation(state["meta_results"], confirm_fn=graph_confirm_fn)
    return {"validation_results": results}


@instrument_agent("Classification Agent")
def classification_node(state: PipelineState) -> dict:
    results = run_classification(state["validation_results"])
    return {"classified_results": results}


@instrument_agent("Report Agent")
def report_node(state: PipelineState) -> dict:
    result = run_report(
        state["meta_results"], state["classified_results"],
        join_results=state.get("join_results"), input_file_path=state["input_file"],
    )
    return {"report_excel_path": result["excel_path"], "report_stats": result["stats"]}


# ── 그래프 빌드 ──────────────────────────────────────────────
def build_graph():
    graph = StateGraph(PipelineState)

    graph.add_node("parsing", parsing_node)
    graph.add_node("meta_init", meta_init_node)
    graph.add_node("meta_exact_check", meta_exact_check_node)
    graph.add_node("meta_retrieve", meta_retrieve_node)
    graph.add_node("meta_no_match", meta_no_match_node)
    graph.add_node("meta_judge", meta_judge_node)
    graph.add_node("meta_retry", meta_retry_node)
    graph.add_node("meta_human_confirm", meta_human_confirm_node)
    graph.add_node("meta_table_disambiguation", meta_table_disambiguation_node)
    graph.add_node("join_resolution", join_resolution_node)
    graph.add_node("db_validation", db_validation_node)
    graph.add_node("classification", classification_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "parsing")
    graph.add_edge("parsing", "meta_init")
    graph.add_edge("meta_init", "meta_exact_check")

    graph.add_conditional_edges(
        "meta_exact_check", route_after_exact,
        {"search": "meta_retrieve", "next_column": "meta_exact_check", "done": "join_resolution",
         "disambiguate": "meta_table_disambiguation"},
    )
    graph.add_conditional_edges(
        "meta_retrieve", route_after_retrieve,
        {"judge": "meta_judge", "no_candidates": "meta_no_match"},
    )
    graph.add_conditional_edges(
        "meta_judge", route_by_judgment,
        {"retry": "meta_retry", "human_confirm": "meta_human_confirm"},
    )
    graph.add_edge("meta_retry", "meta_retrieve")

    for node_name in ("meta_human_confirm", "meta_no_match", "meta_table_disambiguation"):
        graph.add_conditional_edges(
            node_name, route_after_resolution,
            {"next_column": "meta_exact_check", "done": "join_resolution"},
        )

    graph.add_edge("join_resolution", "db_validation")
    graph.add_edge("db_validation", "classification")
    graph.add_edge("classification", "report")
    graph.add_edge("report", END)

    return graph


def get_compiled_graph():
    """checkpointer가 연결된 컴파일된 그래프 반환 (SqliteSaver context manager 필요)"""
    graph = build_graph()
    conn_cm = SqliteSaver.from_conn_string(CHECKPOINT_DB_PATH)
    return graph, conn_cm