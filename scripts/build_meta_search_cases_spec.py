"""
scripts/build_meta_search_cases_spec.py

Meta Search Agent의 매칭 분기(ambiguous / LLM judgment / no_match)를 테스트하기 위한
입력 명세서(data/meta_search_cases_spec.xlsx)를 생성한다.

- mobile_number: 이 데이터셋에서 6개 테이블 전부에 동일하게 존재하는 컬럼명 ->
  exact_match_meta_db가 ambiguous로 반환 -> table_disambiguation_confirmation HITL
- total_recharge_amt: 실제 컬럼(raw_telecom_fact_recharge_voice.total_rech_amt)과
  이름은 다르지만 뜻은 같음 -> exact match 실패, retrieve_candidates가 후보를 찾고
  generate_match_judgment(gpt-5-mini, Structured Output)이 판단 -> confidence와 무관하게
  항상 human_confirm으로 라우팅됨(추정 매칭은 자동 확정 경로가 없음). confidence가
  높으면(HIGH_CONFIDENCE_SKIP_RETRY=0.92 이상) 재검색 없이, 그 사이면 재검색 후 담당자 확인으로 감
- satellite_uptime_ratio: 통신 도메인과 무관한 컬럼명 -> retrieve_candidates가 후보를
  0개 반환하는 것을 실측 확인함 -> unresolved(no_match)로 즉시 종결
"""

import os

import pandas as pd

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "meta_search_cases_spec.xlsx")

rows = [
    ["영문명", "한글명", "항목설명", "type", "시점(기간)"],
    ["mobile_number", "이동전화번호", "고객 식별용 이동전화번호입니다.", "BIGINT", None],
    ["total_recharge_amt", "총충전금액", "고객이 그동안 충전한 총 금액입니다.", "VARCHAR", None],
    ["satellite_uptime_ratio", "위성가동률", "인공위성의 가동률을 나타내는 값입니다.", "DOUBLE", None],
]

df = pd.DataFrame(rows)
df.to_excel(OUT_PATH, index=False, header=False)
print(f"작성 완료: {OUT_PATH}")
