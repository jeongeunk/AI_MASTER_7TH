"""
SchemaScout LangGraph 파이프라인

5개 Agent(Parsing -> Meta Search -> DB Validation -> Classification -> Report)를
하나의 StateGraph로 연결하고, 담당자 확인(inferred/type 불일치)을 실제 interrupt()로 처리.

체크포인터로 SqliteSaver를 사용해 interrupt 대기 상태가 프로세스 재시작 후에도 유지됨.
"""

import os
import sys
from typing import TypedDict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.checkpoint.sqlite import SqliteSaver

from agents.parsing_agent import run_parsing
from agents.meta_search_agent import run_meta_search
from agents.db_validation_agent import run_db_validation
from agents.classification_agent import run_classification
from agents.report_agent import run_report
from llm_client import embed

load_dotenv()

CHECKPOINT_DB_PATH = os.environ.get("CHECKPOINT_DB_PATH", "./db/langgraph_checkpoints.sqlite")
os.makedirs(os.path.dirname(CHECKPOINT_DB_PATH), exist_ok=True)


# ── State 정의 ───────────────────────────────────────────────
class PipelineState(TypedDict, total=False):
    input_file: str
    parsed_rows: list
    meta_results: list
    validation_results: list
    classified_results: list
    report_excel_path: Optional[str]
    report_stats: Optional[dict]


# ── interrupt() 기반 confirm_fn ────────────────────────────
def graph_confirm_fn(payload: dict) -> str:
    """
    LangGraph 노드 안에서 호출되는 확인 콜백.
    interrupt(payload)를 호출하면 그래프 실행이 여기서 멈추고,
    바깥(run_pipeline.py)에서 Command(resume=답변)으로 재개될 때까지 대기.
    """
    decision = interrupt(payload)
    return decision


# ── 노드 정의 ────────────────────────────────────────────────
def parsing_node(state: PipelineState) -> dict:
    result = run_parsing(state["input_file"])
    return {"parsed_rows": result["parsed_rows"]}


def meta_search_node(state: PipelineState) -> dict:
    results = run_meta_search(state["parsed_rows"], embed, confirm_fn=graph_confirm_fn)
    return {"meta_results": results}


def db_validation_node(state: PipelineState) -> dict:
    results = run_db_validation(state["meta_results"], confirm_fn=graph_confirm_fn)
    return {"validation_results": results}


def classification_node(state: PipelineState) -> dict:
    results = run_classification(state["validation_results"])
    return {"classified_results": results}


def report_node(state: PipelineState) -> dict:
    result = run_report(state["meta_results"], state["classified_results"], input_file_path=state["input_file"])
    return {"report_excel_path": result["excel_path"], "report_stats": result["stats"]}


# ── 그래프 빌드 ──────────────────────────────────────────────
def build_graph():
    graph = StateGraph(PipelineState)

    graph.add_node("parsing", parsing_node)
    graph.add_node("meta_search", meta_search_node)
    graph.add_node("db_validation", db_validation_node)
    graph.add_node("classification", classification_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "parsing")
    graph.add_edge("parsing", "meta_search")
    graph.add_edge("meta_search", "db_validation")
    graph.add_edge("db_validation", "classification")
    graph.add_edge("classification", "report")
    graph.add_edge("report", END)

    return graph


def get_compiled_graph():
    """checkpointer가 연결된 컴파일된 그래프 반환 (SqliteSaver context manager 필요)"""
    graph = build_graph()
    conn_cm = SqliteSaver.from_conn_string(CHECKPOINT_DB_PATH)
    return graph, conn_cm