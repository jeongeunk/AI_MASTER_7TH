"""
scripts/build_db_validation_period_cases_spec.py

DB Validation Agent와 Classification Agent의 최종 태그 6종 중, 아직 별도 스펙으로
못 만들어봤던 not_found / type_mismatch / confirm_period / period_mismatch를
테스트하기 위한 입력 명세서(data/db_validation_period_cases_spec.xlsx)를 생성한다.

영문명은 전부 메타DB 컬럼명과 정확히 일치시켜(exact match) Meta Search 단계에서
곧장 넘어가도록 했다 - 이 스펙의 목적은 그 다음 단계(타입/기간 검증)이기 때문.

각 테이블의 실측 보유기간(db/schemascout_data.duckdb 기준):
  fact_call_outgoing  202406~202510 (제일 짧음)
  fact_data_usage     202406~202506
  fact_recharge_data  202406~202512
  fact_recharge_voice 202406~202512
  fact_call_incoming  202406~202512

행 구성:
  1. legacy_churn_flag : 메타DB엔 있지만 실 DB엔 없는 유령 컬럼(scripts/fill_meta_db.py의
     PHANTOM_COLUMN_INJECT로 심어둠) -> not_found
  2. onnet_mou (fact_call_outgoing) : 메타DB엔 INTEGER로 잘못 기재돼 있음(실제 DOUBLE)
     -> type_mismatch HITL. 요청기간 미기재 -> 승인 시 full_period도 같이 확인 가능
  3. total_rech_data (fact_recharge_data) : 메타DB엔 DOUBLE로 잘못 기재(실제 BIGINT)
     -> type_mismatch HITL. 요청기간 202501~202606 -> 승인 시 confirm_period(겹치는 구간만)
  4. vol_3g_mb (fact_data_usage, 202506까지) : 메타DB엔 INTEGER로 잘못 기재(실제 DOUBLE)
     -> type_mismatch HITL. 요청기간 202601~202612(전체 범위 밖) -> 승인 시 period_mismatch
  5. total_ic_mou (fact_call_incoming) : 타입 불일치 없는 깔끔한 컬럼. 요청기간
     202601~202612 -> 클릭 없이 곧장 period_mismatch만 순수하게 확인
"""

import os

import pandas as pd

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "db_validation_period_cases_spec.xlsx"
)

rows = [
    ["영문명", "한글명", "항목설명", "type", "시점(기간)"],
    ["legacy_churn_flag", "레거시이탈플래그", "과거 시스템에서 쓰이던 이탈 플래그 컬럼입니다.", "INTEGER", None],
    ["onnet_mou", "동일망통화시간", "동일망 통화시간을 나타냅니다.", "INTEGER", None],
    ["total_rech_data", "총충전데이터량", "총 충전한 데이터 용량을 나타냅니다.", "DOUBLE", "202501~202606"],
    ["vol_3g_mb", "3G사용량", "3G 사용량(MB)을 나타냅니다.", "INTEGER", "202601~202612"],
    ["total_ic_mou", "총수신통화시간", "총 수신 통화시간을 나타냅니다.", "DOUBLE", "202601~202612"],
]

df = pd.DataFrame(rows)
df.to_excel(OUT_PATH, index=False, header=False)
print(f"작성 완료: {OUT_PATH}")
