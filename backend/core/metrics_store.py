"""
backend/core/metrics_store.py

파이프라인 실행 결과를 컬럼 단위로 감사 DB(schemascout_audit.sqlite)에 영속 저장한다.
run_metrics 테이블 하나로 아래 4개 KPI를 SQL 집계로 뽑아낼 수 있게 하는 것이 목적이다
(산출물/문제 정의 및 서비스 기획_0827.md의 "정량적 목표(KPI)" 표 참고).

- 명세서 1건당 검증 소요 시간: run_started_at/run_completed_at 차이
- 자동 판별 커버리지: final_tag가 채워진 행 수 ÷ 전체 행 수(= Parsing 인정 + Join 추가 컬럼 수,
  meta_results 자체가 이미 이 두 집합의 합이므로 별도 분모 계산 불필요)
- 판정 재현성: 동일 eng_name의 column_id가 thread_id(실행)마다 같은지 비교
- 컬럼 재등장 시 LLM 호출(재검색) 감소율: 같은 eng_name의 retrieval_attempts를
  실행 시점(run_started_at) 순으로 비교

이전에는 이 정보가 PipelineRun(인메모리, 프로세스 재시작 시 소실)이나 최종 리포트
(엑셀 1건씩, 재검색 횟수 등 처리 과정 정보 없음)에만 있어 실행 간 비교가 불가능했다.
"""

import os
import sqlite3
import time

AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")


def _ensure_run_metrics_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS run_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT,
            input_file TEXT,
            eng_name TEXT,
            column_id TEXT,
            match_status TEXT,
            resolution_path TEXT,
            retrieval_attempts INTEGER,
            run_started_at REAL,
            run_completed_at REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # final_tag(KPI2 계측용)는 나중에 추가된 컬럼이라, 이미 만들어진(구버전 스키마) 감사 DB에는
    # CREATE TABLE IF NOT EXISTS로 새로 안 생긴다 - 있는지 확인 후 없으면 ALTER TABLE로 보강한다.
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(run_metrics)").fetchall()}
    if "final_tag" not in existing_cols:
        con.execute("ALTER TABLE run_metrics ADD COLUMN final_tag TEXT")


def log_run_metrics(thread_id: str, input_file: str, meta_results: list,
                     run_started_at: float, run_completed_at: float,
                     report_rows: list = None) -> None:
    """meta_results의 컬럼별 결과를 run_metrics에 한 행씩 적재한다.
    같은 (thread_id, eng_name) 조합이 이미 있으면 재적재하지 않는다 - LangGraph는
    interrupt() 재개 시 노드를 다시 실행할 수 있어, 같은 thread_id로 동일 컬럼이
    중복 기록되는 것을 막기 위한 멱등 처리다.

    report_rows: report_agent.run_report()가 반환하는 "rows"(영문명별 최종태그) -
    KPI2(자동 판별 커버리지) 계측용. Report Agent까지 실행되지 않은 경우(예: 파이프라인
    중간에 오류) None일 수 있으며, 이 경우 final_tag는 NULL로 남는다 - 그 자체로
    "최종태그가 확정되지 못한 컬럼"을 뜻하므로 커버리지 계산에서 정확히 반영된다.
    """
    eng_names = [r.get("영문명") for r in meta_results if r.get("영문명")]
    if not eng_names:
        return

    final_tag_by_eng_name = {
        r.get("영문명"): r.get("최종태그") for r in (report_rows or []) if r.get("영문명")
    }

    con = sqlite3.connect(AUDIT_DB_PATH)
    _ensure_run_metrics_table(con)
    try:
        existing = {
            row[0] for row in con.execute(
                "SELECT eng_name FROM run_metrics WHERE thread_id = ?", [thread_id]
            ).fetchall()
        }
        for row in meta_results:
            eng_name = row.get("영문명")
            if not eng_name or eng_name in existing:
                continue
            meta_row = row.get("meta_row") or {}
            con.execute(
                "INSERT INTO run_metrics "
                "(thread_id, input_file, eng_name, column_id, match_status, resolution_path, "
                " retrieval_attempts, run_started_at, run_completed_at, final_tag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [thread_id, input_file, eng_name, meta_row.get("column_id"),
                 row.get("match_status"), row.get("resolution_path"),
                 row.get("retrieval_attempts"), run_started_at, run_completed_at,
                 final_tag_by_eng_name.get(eng_name)],
            )
            existing.add(eng_name)
        con.commit()
    finally:
        con.close()


def _ensure_token_usage_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT,
            input_file TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def log_token_usage(thread_id: str, input_file: str, calls: list) -> None:
    """LLM/임베딩 호출 1건당 한 줄씩 적재한다(run_metrics와 달리 멱등 처리를 하지 않는다 -
    실제로 호출된 횟수 그대로가 의미 있는 값이기 때문).

    calls: [{"model": str, "tokens": {"prompt": int, "completion": int, "total": int}}, ...]
    tool_span이 기록한 tool_calls 중 tokens가 있는 것만 골라 넘기면 된다.
    """
    if not calls:
        return

    con = sqlite3.connect(AUDIT_DB_PATH)
    _ensure_token_usage_table(con)
    try:
        for call in calls:
            tokens = call.get("tokens")
            if not tokens:
                continue
            con.execute(
                "INSERT INTO token_usage "
                "(thread_id, input_file, model, prompt_tokens, completion_tokens, total_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [thread_id, input_file, call.get("model"),
                 tokens.get("prompt"), tokens.get("completion"), tokens.get("total")],
            )
        con.commit()
    finally:
        con.close()


def _ensure_kpi_baseline_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS kpi_baseline (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            marked_at REAL NOT NULL
        )
    """)


def set_kpi_baseline(marked_at: float = None) -> float:
    """화면의 KPI 리포트가 이 시점 이후 run_metrics만 집계하도록 기준 시각을 기록한다.
    기존 이력(run_metrics)은 그대로 두고, "여기서부터가 새 테스트 라운드"라는 표시만
    남기는 방식 - 데이터를 지우지 않아 되돌리기 쉽다. marked_at을 안 넘기면 지금 이
    순간(time.time())을 기준으로 삼는다. 행 1개(id=1)만 유지 - 다시 호출하면 기준
    시점이 그 값으로 교체된다(이전 기준은 남지 않음)."""
    marked_at = marked_at if marked_at is not None else time.time()
    con = sqlite3.connect(AUDIT_DB_PATH)
    _ensure_kpi_baseline_table(con)
    try:
        con.execute(
            "INSERT INTO kpi_baseline (id, marked_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET marked_at = excluded.marked_at",
            [marked_at],
        )
        con.commit()
    finally:
        con.close()
    return marked_at


def get_kpi_baseline() -> float:
    """기록된 기준 시점(epoch)을 반환. 아직 한 번도 기록한 적 없으면 None -
    이 경우 KPI 리포트는 기존처럼 전체 이력을 집계한다(하위 호환)."""
    con = sqlite3.connect(AUDIT_DB_PATH)
    _ensure_kpi_baseline_table(con)
    try:
        row = con.execute("SELECT marked_at FROM kpi_baseline WHERE id = 1").fetchone()
    finally:
        con.close()
    return row[0] if row else None


def clear_kpi_baseline() -> None:
    """기준 시점을 해제한다 - 이후 KPI 리포트는 다시 전체 이력을 집계한다."""
    con = sqlite3.connect(AUDIT_DB_PATH)
    _ensure_kpi_baseline_table(con)
    try:
        con.execute("DELETE FROM kpi_baseline WHERE id = 1")
        con.commit()
    finally:
        con.close()


def get_token_usage_summary(thread_id: str) -> dict:
    """콘솔 출력용 - 이 thread_id가 지금까지 쓴 토큰을 모델별로 합산해서 반환한다."""
    con = sqlite3.connect(AUDIT_DB_PATH)
    _ensure_token_usage_table(con)
    try:
        rows = con.execute(
            "SELECT model, SUM(total_tokens) FROM token_usage WHERE thread_id = ? GROUP BY model",
            [thread_id],
        ).fetchall()
    finally:
        con.close()
    by_model = {model: total for model, total in rows}
    return {"total": sum(by_model.values()), "by_model": by_model}
