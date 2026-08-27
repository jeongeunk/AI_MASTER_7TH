"""
backend/core/metrics_store.py

파이프라인 실행 결과를 컬럼 단위로 감사 DB(schemascout_audit.sqlite)에 영속 저장한다.
run_metrics 테이블 하나로 아래 4개 KPI를 SQL 집계로 뽑아낼 수 있게 하는 것이 목적이다
(산출물/문제 정의 및 서비스 기획_0827.md의 "정량적 목표(KPI)" 표 참고).

- 명세서 1건당 검증 소요 시간: run_started_at/run_completed_at 차이
- 자동 판별 커버리지: match_status/resolution_path 분포
- 판정 재현성: 동일 eng_name의 column_id가 thread_id(실행)마다 같은지 비교
- 컬럼 재등장 시 LLM 호출(재검색) 감소율: 같은 eng_name의 retrieval_attempts를
  실행 시점(run_started_at) 순으로 비교

이전에는 이 정보가 PipelineRun(인메모리, 프로세스 재시작 시 소실)이나 최종 리포트
(엑셀 1건씩, 재검색 횟수 등 처리 과정 정보 없음)에만 있어 실행 간 비교가 불가능했다.
"""

import os
import sqlite3

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


def log_run_metrics(thread_id: str, input_file: str, meta_results: list,
                     run_started_at: float, run_completed_at: float) -> None:
    """meta_results의 컬럼별 결과를 run_metrics에 한 행씩 적재한다.
    같은 (thread_id, eng_name) 조합이 이미 있으면 재적재하지 않는다 - LangGraph는
    interrupt() 재개 시 노드를 다시 실행할 수 있어, 같은 thread_id로 동일 컬럼이
    중복 기록되는 것을 막기 위한 멱등 처리다.
    """
    eng_names = [r.get("영문명") for r in meta_results if r.get("영문명")]
    if not eng_names:
        return

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
                " retrieval_attempts, run_started_at, run_completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [thread_id, input_file, eng_name, meta_row.get("column_id"),
                 row.get("match_status"), row.get("resolution_path"),
                 row.get("retrieval_attempts"), run_started_at, run_completed_at],
            )
            existing.add(eng_name)
        con.commit()
    finally:
        con.close()
