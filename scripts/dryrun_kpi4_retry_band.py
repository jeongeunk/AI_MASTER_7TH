# -*- coding: utf-8 -*-
"""
scripts/dryrun_kpi4_retry_band.py

KPI4 측정 명세서(data/KPI4_재검색감소_측정.xlsx)의 "사전 검증" 전용.

확인 목표
--------
1회차(Episodic Memory가 비어 있는 상태)에서 각 측정 대상 행의
generate_match_judgment confidence가 재검색 구간 [RETRY_CONFIDENCE_FLOOR(0.70),
HIGH_CONFIDENCE_SKIP_RETRY(0.92)) 에 들어가는지 = decide_route가 "retry"를
돌려주는지 = retrieval_attempts가 1 이상 찍히는지 확인한다.

 - 대부분 attempts>=1  -> 시나리오 성립(2·3회차에서 0으로 줄어드는지 볼 수 있음)
 - 대부분 attempts==0  -> 설명이 너무 쉬움. 더 뭉툭하게/영문명을 더 우회적으로
 - no_match 다수        -> 패러프레이즈가 너무 멀어짐. 정답 쪽으로 당기기

격리
----
실데이터 메타 DB를 건드리지 않도록, META_DB_PATH / AUDIT_DB_PATH 를 반드시
사본으로 지정해서 실행한다(아래 usage 참고). auto_confirm은 정답키(JSON)에
있는 컬럼으로만 승인한다(Lessons Learned ⑤: 승인 시 영구 저장되는 테스트는
정답으로만 승인 + 사본에서).

usage (백엔드를 잠깐 내린 뒤):
    cp db/schemascout_meta.duckdb   /tmp/meta_copy.duckdb
    cp db/schemascout_audit.sqlite  /tmp/audit_copy.sqlite   # 없으면 생략 가능
    META_DB_PATH=/tmp/meta_copy.duckdb AUDIT_DB_PATH=/tmp/audit_copy.sqlite \
        venv/Scripts/python.exe scripts/dryrun_kpi4_retry_band.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(BASE, "data", "KPI4_재검색감소_측정.xlsx")
KEY = os.path.join(BASE, "data", "KPI4_재검색감소_측정.정답키.json")
OUT = os.path.join(BASE, "dryrun_kpi4_retry_band_result.txt")

if "meta_copy" not in os.environ.get("META_DB_PATH", "") and "--force-real-db" not in sys.argv:
    print("거부: META_DB_PATH가 사본이 아닙니다. 실데이터 DB 오염 방지를 위해 사본 경로로만 실행하세요.")
    print("     (정말 실 DB로 돌리려면 --force-real-db 플래그를 명시)")
    sys.exit(2)

from agents.meta_search_agent import (
    HIGH_CONFIDENCE_SKIP_RETRY,
    RETRY_CONFIDENCE_FLOOR,
    run_meta_search,
)
from agents.parsing_agent import run_parsing
from llm_client import embed

answer_key = json.load(open(KEY, encoding="utf-8"))
lines = []


def log(s=""):
    print(s)
    lines.append(str(s))


def auto_confirm(payload):
    ptype = payload.get("type")
    if ptype == "header_row_confirmation":
        pv = payload.get("row_previews", [])
        return {"decision": "approved", "selected_row_idx": pv[1]["row_idx"] if len(pv) > 1 else 0}
    if ptype == "header_mapping_confirmation":
        sug = payload.get("suggested_column")
        return {"decision": "approved", "selected_column": sug} if sug else {"decision": "rejected"}
    if ptype == "row_completion_confirmation":
        return {"decision": "approved", "approved_row_indices": [c["row_index"] for c in payload["candidates"]]}
    if ptype == "inferred_confirmation":
        # 정답키에 있는 컬럼으로만 승인 - 후보 목록에서 정답 table.column을 찾아 선택
        eng = (payload.get("column_info") or {}).get("영문명") or payload.get("eng_name")
        truth = answer_key.get(eng)
        for c in payload.get("candidates", []):
            cid = c.get("column_id") or ""
            if truth and cid.endswith("." + truth):
                return {"decision": "approved", "selected_column_id": cid}
        return "approved"  # 정답 후보가 안 보이면 그냥 승인(관찰만이 목적)
    return {"decision": "rejected"}


log("=" * 78)
log("KPI4 사전 검증 - 1회차(Episodic Memory 비어있음 가정) 재검색 구간 진입 여부")
log(f"재검색 구간: [{RETRY_CONFIDENCE_FLOOR}, {HIGH_CONFIDENCE_SKIP_RETRY})   META_DB_PATH={os.environ.get('META_DB_PATH')}")
log("=" * 78)

parsed = run_parsing(SPEC, confirm_fn=auto_confirm)
log(f"parsed_rows={len(parsed['parsed_rows'])}  failed_rows={len(parsed['failed_rows'])}")

results = run_meta_search(parsed["parsed_rows"], embed, confirm_fn=auto_confirm)

hdr = f"\n{'영문명':30} {'정답':16} {'status':20} {'conf':>6} {'att':>4}  band?  matched"
log(hdr)
log("-" * 100)
retry_cnt = exact_cnt = zero_cnt = nomatch_cnt = 0
for r in results:
    eng = r.get("영문명") or ""
    truth = answer_key.get(eng, "-")
    status = r.get("match_status") or ""
    conf = r.get("llm_confidence")
    att = r.get("retrieval_attempts")
    mr = r.get("meta_row") or {}
    matched = f"{mr.get('table_id','')}.{mr.get('column_name','')}" if mr else ""
    in_band = conf is not None and RETRY_CONFIDENCE_FLOOR <= conf < HIGH_CONFIDENCE_SKIP_RETRY
    confs = f"{conf:.3f}" if isinstance(conf, (int, float)) else "  -  "
    log(f"{eng:30} {truth:16} {status:20} {confs:>6} {str(att):>4}  {'YES ' if in_band else '   -'}  {matched}")
    if status == "matched":
        exact_cnt += 1
    elif status in ("unresolved",) and (not matched):
        nomatch_cnt += 1
    if isinstance(att, int):
        if att >= 1:
            retry_cnt += 1
        elif att == 0 and status != "matched":
            zero_cnt += 1

n_target = sum(1 for r in results if (r.get("영문명") or "") in answer_key
               and (r.get("영문명") or "") not in ("total_ic_mou", "CumulativeRechargeAmount"))
log("\n" + "=" * 78)
log(f"측정 대상(12): 재검색 발생(attempts>=1) {retry_cnt}건 / 재검색 없음(attempts=0, 추정매칭) {zero_cnt}건 / no_match {nomatch_cnt}건")
log(f"대조군: matched(정확매칭) {exact_cnt}건")
log("판정 기준: 측정 대상 12건 중 attempts>=1 이 8건 이상이면 시나리오 성립. "
    "0건이 많으면 설명을 더 뭉툭하게, no_match가 많으면 패러프레이즈를 정답 쪽으로.")
log("=" * 78)

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print(f"\n결과 저장: {OUT}")
