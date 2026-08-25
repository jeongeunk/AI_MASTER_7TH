"""
scripts/build_join_test_spec.py

Join Resolution Agent를 테스트하기 위한 입력 명세서(data/join_test_spec.xlsx)를 생성한다.
4개의 서로 다른 테이블(raw_telecom_dim_customer, fact_data_usage, fact_call_incoming,
fact_call_outgoing)에 걸쳐 컬럼을 요청하도록 구성 - 실 DB 기준 각 테이블의 보유기간이
서로 달라(fact_data_usage는 202506까지, fact_call_outgoing은 202510까지, 나머지는
202512까지) "조인 가능성 검증" 시트에서 교집합 계산이 실제로 값어치 있다는 게 눈에 보인다.

영문명은 메타DB 컬럼명과 정확히 일치시켜서(exact match) Meta Search 단계의 LLM/담당자
확인 없이 곧장 Join Resolution 단계로 넘어가도록 했다 - 이 스펙의 목적은 새로 추가된
조인 검증 로직을 관찰하는 것이지 파싱/매칭 LLM 경로 재확인이 아니기 때문
(그건 data/llm_pathway_spec.xlsx가 이미 담당).

table_relationships에 이미 dim_customer <-> 각 fact 테이블 관계가 등록돼 있으므로,
아래 조합은 다음과 같이 처리된다:
  - dim_customer <-> fact_*  : 직접 등록된 관계 그대로 사용 (declared)
  - fact_* <-> fact_*        : 직접 관계는 없지만 dim_customer를 경유하는 2-hop으로 자동 해결 (declared)
즉 이 스펙만으로는 "추정 후보 생성 + 값 overlap 검증 + 담당자 확인(HITL)" 경로까지는
실제 UI에서 보기 어렵다 (현재 6개 테이블이 전부 dim_customer로 연결돼 있어서
2-hop 경로가 항상 존재함) - 그 경로는 scripts/verify_join_resolution.py의 [3]번
시나리오(관계 그래프를 강제로 비운 상태)로 별도 검증돼 있다.
"""

import os

import pandas as pd

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "join_test_spec.xlsx")

rows = [
    ["영문명", "한글명", "항목설명", "type", "시점(기간)"],
    ["mobile_number", "이동전화번호", "고객 식별용 이동전화번호입니다.", "BIGINT", None],
    ["circle_id", "권역코드", "통신 서비스 제공 지역의 권역 코드입니다.", "BIGINT", None],
    ["vol_3g_mb", "3G사용량", "3G 사용량(MB)을 나타냅니다.", "INTEGER", "202401~202412"],
    ["total_ic_mou", "총수신통화시간", "총 수신 통화시간을 나타냅니다.", "DOUBLE", None],
    ["total_og_mou", "총발신통화시간", "총 발신 통화시간을 나타냅니다.", "DOUBLE", "202401~202412"],
]

df = pd.DataFrame(rows)
df.to_excel(OUT_PATH, index=False, header=False)
print(f"작성 완료: {OUT_PATH}")
print("포함 테이블: raw_telecom_dim_customer(mobile_number, circle_id), "
      "raw_telecom_fact_data_usage(vol_3g_mb), raw_telecom_fact_call_incoming(total_ic_mou), "
      "raw_telecom_fact_call_outgoing(total_og_mou)")
