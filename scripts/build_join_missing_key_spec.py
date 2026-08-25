"""
scripts/build_join_missing_key_spec.py

Join Resolution Agent의 "조인키가 요청 목록에 없음" 알림 + HITL(missing_join_key_confirmation)
+ 승인 시 자동 추가 흐름을 테스트하기 위한 입력 명세서(data/join_missing_key_spec.xlsx)를 생성한다.

data/join_test_spec.xlsx와 같은 4개 테이블(dim_customer/fact_data_usage/fact_call_incoming/
fact_call_outgoing)을 요청하지만, 조인키인 mobile_number는 의도적으로 제외했다.
Join Resolution 단계에서 4개 테이블이 전부 조인 가능하다고 확정되지만(dim_customer가
전부와 직접 연결돼 있음), 그 조인에 쓰이는 mobile_number 자체는 요청 목록에 없으므로
missing_join_key_confirmation이 정확히 1번 뜬다(3개 쌍이 전부 같은 키를 필요로 해도
중복으로 묻지 않음). 승인하면 mobile_number가 dim_customer 소속으로 자동 추가되어
최종 리포트에 "조인키(자동 추가)" 행으로 나온다.
"""

import os

import pandas as pd

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "join_missing_key_spec.xlsx")

rows = [
    ["영문명", "한글명", "항목설명", "type", "시점(기간)"],
    ["circle_id", "권역코드", "통신 서비스 제공 지역의 권역 코드입니다.", "BIGINT", None],
    ["vol_3g_mb", "3G사용량", "3G 사용량(MB)을 나타냅니다.", "INTEGER", "202401~202412"],
    ["total_ic_mou", "총수신통화시간", "총 수신 통화시간을 나타냅니다.", "DOUBLE", None],
    ["total_og_mou", "총발신통화시간", "총 발신 통화시간을 나타냅니다.", "DOUBLE", "202401~202412"],
]

df = pd.DataFrame(rows)
df.to_excel(OUT_PATH, index=False, header=False)
print(f"작성 완료: {OUT_PATH}")
print("포함 테이블: dim_customer(circle_id), fact_data_usage(vol_3g_mb), "
      "fact_call_incoming(total_ic_mou), fact_call_outgoing(total_og_mou) - mobile_number 의도적 제외")
