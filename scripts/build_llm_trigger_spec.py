"""
scripts/build_llm_trigger_spec.py

Parsing Agent와 Meta Search Agent의 LLM fallback 경로를 실제로 타도록 설계한
테스트용 입력 명세서(data/llm_pathway_spec.xlsx)를 생성한다.

의도적으로 만든 4가지 트리거:
1. 헤더 "행" 판별 애매 -> select_header_row_with_llm (gpt-4.1-mini)
   0행에 표준 필드 키워드가 다수 섞인 "설명 문장"을 둬서, 실제 헤더 행(1행)과
   함께 hits>=3인 후보가 2개가 되게 만든다.
2. 헤더 "필드" 매핑 실패 -> generate_header_mapping_judgment (gpt-4.1-mini)
   영문명/한글명 헤더명을 규칙 키워드에 안 걸리는 "필드코드"/"표시명"으로 바꾸고,
   샘플값(영문 snake_case identifier / 한글 명칭)으로만 구분 가능하게 한다.
3. 행 보완 LLM 추론 -> infer_name_fields_with_llm (gpt-4.1-mini)
   항목설명만 있고 영문명/한글명이 비어 있으며, 메타DB에 없는(=임베딩 후보가 안 나오는)
   도메인 밖 개념(대리점 방문 지점 코드)을 넣어 메타DB 매칭이 실패하게 한다.
4. Meta Search 판단 LLM -> generate_match_judgment (gpt-5-mini)
   실제 메타DB 컬럼과 "설명은 같은 뜻이지만 영문명이 정확히 일치하지 않는" 행을 넣어
   exact_match는 실패하지만 임베딩/퍼지 후보는 잡히게 한다.
"""

import os
import sys

import pandas as pd

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "llm_pathway_spec.xlsx")

rows = [
    # row 0: 헤더 행 판별을 애매하게 만드는 설명 문장 (영문/한글/설명/자료형/기간 키워드 포함 -> hits>=3)
    ["본 문서는 통신 데이터 컬럼의 영문명, 한글명, 항목설명, 자료형, 요구기간 정보를 정리한 명세서입니다.",
     None, None, None, None],
    # row 1: 실제 헤더 행 (표준 키워드에 안 걸리는 라벨로 필드/한글 매핑을 의도적으로 모호하게 함)
    ["필드코드", "표시명", "설명", "타입", "요구기간"],
    # row 2: meta_search LLM 트리거 - total_rech_amt와 뜻은 같지만 영문명이 다름
    ["total_recharge_amt", "총 충전금액", "고객이 그동안 충전한 총 금액입니다.", "VARCHAR", "202401~202412"],
    # row 3: meta_search LLM 트리거 - av_rech_amt_data와 뜻은 비슷하지만 영문명이 다름(애매성 유도)
    ["avg_data_amt", "평균 데이터 충전금액", "고객의 데이터 평균 충전 금액입니다.", "DOUBLE", None],
    # row 4: 행 보완 LLM 트리거 - 영문명/한글명 없음 + 메타DB에 없는 도메인(대리점 방문)
    [None, None, "고객이 최근 방문한 대리점의 지점 코드입니다.", None, None],
    # row 5: 대조군 - 메타DB와 정확히 일치 (LLM 없이 규칙 매칭만으로 처리됨)
    ["mobile_number", "이동전화번호", "고객 식별용 이동전화번호입니다.", "BIGINT", "202401~202412"],
]

df = pd.DataFrame(rows)
df.to_excel(OUT_PATH, index=False, header=False)
print(f"작성 완료: {OUT_PATH}")
