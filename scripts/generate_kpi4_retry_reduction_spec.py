# -*- coding: utf-8 -*-
"""
scripts/generate_kpi4_retry_reduction_spec.py

KPI4(컬럼 재등장 시 재검색 감소율) 전용 측정 명세서를 생성한다.

문제의식
--------
지금까지의 KPI4 실측이 "관찰 불가"로 나온 이유는, 표본 대부분이
  (1) 정확 매칭(`matched`)이라 애초에 retrieval_attempts=0 이거나,
  (2) 1차 판단에서 confidence >= 0.92 가 나와 재검색이 걸리지 않아
"1회차부터 이미 0회"이기 때문이다. 0에서는 더 줄어들 수 없으므로 Episodic
Memory의 재검색 절감 효과를 관찰할 여지가 없다.

decide_route(agents/meta_search_agent.py) 규칙:
    RETRY_CONFIDENCE_FLOOR(0.70) <= confidence < HIGH_CONFIDENCE_SKIP_RETRY(0.92)
    AND retrieval_attempts < MAX_RETRIEVAL_ATTEMPTS(2)   -> "retry"
즉 1차 confidence가 [0.70, 0.92) 구간에 떨어져야만 재검색이 발생한다.

이 명세서의 설계
---------------
"뜻은 매우 가깝고 서로 헷갈리는 컬럼군"(통화시간/MOU 계열 12개)을 골라,
 - 영문명은 실제 메타 DB 컬럼명과 다르게 패러프레이즈(정확 매칭 회피)
 - 항목설명은 의도적으로 짧고 뭉툭하게(여러 컬럼에 두루 맞아 LLM이 확신 못 하도록)
해서, 1차 retrieval + generate_match_judgment 의 confidence가 [0.70, 0.92)
구간에 떨어지도록(= 1회차에 재검색 1~2회가 걸리도록) 유도한다.

측정 프로토콜(문서 본문에 함께 기술)
    0) confirmed_mapping_examples 에서 아래 eng_name 사전 정리(+시각 기록)
    1) 1회차 업로드: 각 행을 "정답 컬럼"으로 승인 -> Episodic Memory 적재
       (정답은 ANSWER_KEY 로 함께 출력 - 자동 승인 시 이 매핑대로 approve)
    2) 2·3회차: 동일 파일 재업로드 -> 확인 사례가 후보 1순위로 들어와
       confidence >= 0.92 -> retrieval_attempts -> 0 으로 감소해야 함
    3) KPI4 = 1회차 attempts>=1 인 행만 대상으로
       mean(1회차 attempts) vs mean(2회차 이후 attempts), 감소율 = 1 - 후자/전자

대조군(_CONTROL)
    - exact_ic: 실제 컬럼명 그대로 -> `matched`, attempts=0, KPI4 집계 제외 대상
    - distinct_recharge: 이웃이 촘촘하지 않은 명확한 컬럼 -> 1차부터 attempts=0 예상
      (재검색 여지 없는 행은 KPI4에서 제외한다는 지표 정의를 보여주는 용도)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HEADER = ["영문명", "한글명", "항목설명", "type", "시점(기간)"]

# ── 측정 대상 12건: 통화시간/MOU 계열(서로 매우 유사) ──────────────────────
# (paraphrased_eng_name, 한글명, 뭉툭한 설명, 정답 메타 DB 컬럼명)
TARGETS = [
    ("TotalInboundTalkMinutes",      "총수신통화분",     "고객의 수신 통화 시간 관련 지표입니다.",          "total_ic_mou"),
    ("TotalOutboundTalkMinutes",     "총발신통화분",     "고객의 발신 통화 시간 관련 지표입니다.",          "total_og_mou"),
    ("LocalInboundTalkMinutes",      "시내수신통화분",   "같은 지역 안에서의 수신 통화 관련 값입니다.",     "loc_ic_mou"),
    ("LocalOutboundTalkMinutes",     "시내발신통화분",   "같은 지역 안에서의 발신 통화 관련 값입니다.",     "loc_og_mou"),
    ("LongDistInboundTalkMinutes",   "시외수신통화분",   "먼 거리 구간의 수신 통화 관련 값입니다.",         "std_ic_mou"),
    ("LongDistOutboundTalkMinutes",  "시외발신통화분",   "먼 거리 구간의 발신 통화 관련 값입니다.",         "std_og_mou"),
    ("IntlInboundTalkMinutes",       "국제수신통화분",   "해외와 주고받은 수신 통화 관련 값입니다.",        "isd_ic_mou"),
    ("IntlOutboundTalkMinutes",      "국제발신통화분",   "해외와 주고받은 발신 통화 관련 값입니다.",        "isd_og_mou"),
    ("RoamInboundTalkMinutes",       "로밍수신통화분",   "로밍 상태에서의 수신 통화 관련 값입니다.",        "roam_ic_mou"),
    ("RoamOutboundTalkMinutes",      "로밍발신통화분",   "로밍 상태에서의 발신 통화 관련 값입니다.",        "roam_og_mou"),
    ("SameNetworkTalkMinutes",       "동일망통화분",     "같은 통신사 안에서의 통화 관련 값입니다.",        "onnet_mou"),
    ("OtherNetworkTalkMinutes",      "타사망통화분",     "다른 통신사와의 통화 관련 값입니다.",             "offnet_mou"),
]

# ── 대조군 2건 ────────────────────────────────────────────────────────────
CONTROL = [
    # 정확 매칭 -> matched, attempts=0, KPI4 제외 대상
    ("total_ic_mou",              "총수신통화시간",   "총 수신 통화시간(MOU)을 나타냅니다.",             "total_ic_mou"),
    # 이웃이 촘촘하지 않은 명확한 추정 매칭 -> 1차부터 attempts=0 예상
    ("CumulativeRechargeAmount",  "누적충전금액",     "고객이 지금까지 충전한 총 금액입니다.",           "total_rech_amt"),
]

PERIOD = "202401~202412"

rows, answer_key = [], {}
for eng, kor, desc, truth in TARGETS + CONTROL:
    rows.append([eng, kor, desc, "DOUBLE", PERIOD])
    answer_key[eng] = truth

NOTE = (
    "본 문서는 KPI4(컬럼 재등장 시 재검색 감소율)를 실제로 측정하기 위한 전용 명세서입니다. "
    "앞 12건(통화시간/MOU 계열)은 서로 매우 유사한 컬럼군을 뜻은 같고 표기만 다른 영문명 + "
    "뭉툭한 설명으로 구성해, 1차 매칭 판단의 confidence가 재검색 구간[0.70, 0.92)에 "
    "떨어지도록(=1회차에 재검색이 걸리도록) 유도합니다. 이 파일을 서로 다른 thread_id로 "
    "3회 반복 업로드하고, 매 회차 각 행을 정답 컬럼으로 승인하면, 2회차부터 Episodic Memory "
    "(과거 확인 사례)가 후보 1순위로 들어와 재검색 횟수가 0으로 줄어드는지 확인할 수 있습니다. "
    "마지막 2건은 대조군(정확 매칭 / 재검색 여지 없는 추정 매칭)으로, KPI4 집계에서 제외됩니다."
)

os.makedirs(OUT_DIR, exist_ok=True)
path = os.path.join(OUT_DIR, "KPI4_재검색감소_측정.xlsx")
pd.DataFrame([[NOTE, None, None, None, None], HEADER] + rows).to_excel(
    path, index=False, header=False, engine="openpyxl"
)
key_path = os.path.join(OUT_DIR, "KPI4_재검색감소_측정.정답키.json")
with open(key_path, "w", encoding="utf-8") as f:
    json.dump(answer_key, f, ensure_ascii=False, indent=2)

print(f"작성 완료: {path} ({len(rows)}행: 측정 {len(TARGETS)} + 대조군 {len(CONTROL)})")
print(f"정답키   : {key_path}")
print("사전 정리 대상 eng_name:", ", ".join(e for e, *_ in TARGETS))
