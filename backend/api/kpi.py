"""
backend/api/kpi.py

GET /api/kpi/report — run_metrics(감사 DB)에 쌓인 웹 업로드 실행 이력을 집계해
KPI1(검증 소요 시간)·KPI2(자동 판별 커버리지)·KPI3(판정 재현성)·
KPI4(컬럼 재등장 시 재검색 감소율) 4개 모두를 JSON으로 반환.

집계 로직은 evaluation/run_metrics_kpi_report.py의 compute_* 함수를 그대로
재사용한다(CLI와 API가 서로 다른 기준으로 집계하지 않도록 하나의 소스만 둔다).

KPI1·2는 실행(thread_id) 단위 지표라 eng_name 쿼리 파라미터의 영향을 받지 않는다
(특정 컬럼만 걸러내면 실행의 분모/처리시간이 왜곡되므로 항상 전체 데이터로 집계).
KPI3·4만 eng_name으로 특정 컬럼 필터링이 가능하다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter

from evaluation.run_metrics_kpi_report import (
    compute_coverage,
    compute_processing_time,
    compute_reproducibility,
    compute_retrieval_attempts,
    fetch_rows,
    get_connection,
    group_by_eng_name,
    group_by_thread_id,
    run_metrics_table_exists,
)

router = APIRouter()


@router.get("/kpi/report")
def get_kpi_report(eng_name: str = None):
    con = get_connection()
    try:
        if not run_metrics_table_exists(con):
            return {
                "available": False,
                "message": "run_metrics 테이블이 아직 없습니다. 웹에서 명세서를 최소 1회 이상 실행한 뒤 다시 확인하세요.",
            }

        all_rows = fetch_rows(con)  # KPI1·2(실행 단위)는 항상 전체 데이터 기준
        if not all_rows:
            return {
                "available": False,
                "message": "run_metrics에 기록된 데이터가 없습니다.",
            }

        thread_grouped = group_by_thread_id(all_rows)
        rows_for_column_kpi = fetch_rows(con, eng_name) if eng_name else all_rows
        grouped = group_by_eng_name(rows_for_column_kpi)

        return {
            "available": True,
            "processing_time": compute_processing_time(thread_grouped),
            "coverage": compute_coverage(thread_grouped),
            "reproducibility": compute_reproducibility(grouped),
            "retrieval_attempts": compute_retrieval_attempts(grouped),
        }
    finally:
        con.close()
