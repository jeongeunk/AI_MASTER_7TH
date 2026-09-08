# -*- coding: utf-8 -*-
"""
scripts/generate_type_mismatch_spec.py (v2)

DB Validation Agent의 타입 불일치(type_mismatch_confirmation) HITL 경로 전용
테스트 명세서. v1 실행 결과 실측을 통해 두 가지를 반영해 재설계했다:

1) 타입 비교는 업로드 명세서의 "type" 칸이 아니라 메타 카탈로그(column_spec.data_type)
   기준이라, 실제 카탈로그-실데이터 불일치가 있는 컬럼만 골라야 한다(전체 75개 중
   자연 발생 불일치는 5건뿐이라, 별도 스크립트로 7개 컬럼의 카탈로그값을 일시적으로
   틀리게 바꿔 총 12건을 만든다 - scripts/apply_type_mismatch_fixtures.py 참고).
2) 여러 테이블에 동시에 존재하는 컬럼(mobile_number/month/aon)은 소속 테이블 선택
   확인이 먼저 끼어들어 순수 타입 불일치 테스트를 방해하므로 제외했다.

영문명은 전부 실제 메타 DB 컬럼명과 정확히 일치(Meta Search 단계 HITL 없음).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HEADER = ["영문명", "한글명", "항목설명", "type", "시점(기간)"]

rows = [
    # --- 자연 발생 불일치 5건(카탈로그 원래 값 자체가 실데이터와 다름) ---
    ["churn_month", "고객이탈월", "고객이 이탈한 월(YYYYMM)이며, 활성 고객은 NULL입니다.", "VARCHAR", None],
    ["onnet_mou", "동일망통화시간", "동일망 통화시간을 나타냅니다.", "INTEGER", None],
    ["vol_3g_mb", "3G사용량", "3G 사용량(MB)을 나타냅니다.", "INTEGER", None],
    ["total_rech_data", "총충전데이터량", "총 충전한 데이터 용량을 나타냅니다.", "DOUBLE", None],
    ["total_rech_amt", "총충전금액", "총 충전한 금액을 나타냅니다.", "VARCHAR", None],
    # --- 일시적으로 만든 불일치 7건(apply_type_mismatch_fixtures.py로 카탈로그 값 변경) ---
    ["total_ic_mou", "총수신통화시간", "총 수신 통화시간을 나타냅니다.", "DOUBLE", None],
    ["total_og_mou", "총발신통화시간", "총 발신 통화시간을 나타냅니다.", "DOUBLE", None],
    ["total_rech_num", "총충전건수", "고객이 충전한 총 건수를 나타냅니다.", "BIGINT", None],
    ["max_rech_amt", "최대충전금액", "최대로 충전한 금액을 나타냅니다.", "DOUBLE", None],
    ["count_rech_2g", "2G충전횟수", "2G 데이터 충전 횟수를 나타냅니다.", "BIGINT", None],
    ["fb_user", "페이스북데이터이용여부", "페이스북 무료 또는 우대 데이터 서비스 이용 여부입니다.", "BIGINT", None],
    ["circle_id", "권역코드", "통신 서비스 제공 지역의 권역 코드입니다.", "BIGINT", None],
    # --- 대조군 2건(카탈로그-실데이터 일치, 불일치가 뜨면 안 됨) ---
    ["vol_2g_mb", "2G사용량", "2G 사용량(MB)을 나타냅니다.", "DOUBLE", None],
    ["arpu", "가입자평균매출", "해당 월 가입자 1인당 평균 매출입니다.", "DOUBLE", None],
]

path = os.path.join(OUT_DIR, "타입불일치_테스트.xlsx")
note = (
    "본 문서는 DB Validation Agent의 타입 불일치 담당자 확인 경로 전용 테스트 명세서입니다"
    "(v2 - 메타 카탈로그 기준 비교임을 반영해 재설계). 앞 5건은 데이터셋에 자연적으로 "
    "존재하는 카탈로그-실데이터 불일치, 다음 7건은 검증을 위해 카탈로그 값을 일시적으로 "
    "바꿔 만든 불일치(테스트 후 원복 예정), 마지막 2건은 대조군(불일치 없이 정상 통과해야 "
    "함)입니다. mobile_number/month/aon처럼 여러 테이블에 동시에 존재하는 컬럼은 소속 "
    "테이블 확인이 먼저 끼어들어 제외했습니다."
)
df = pd.DataFrame([[note, None, None, None, None], HEADER] + rows)
df.to_excel(path, index=False, header=False, engine="openpyxl")
print(f"작성 완료: {path} ({len(rows)}행 데이터)")
