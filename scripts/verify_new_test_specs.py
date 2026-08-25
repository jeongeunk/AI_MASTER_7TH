"""
scripts/verify_new_test_specs.py

신규 스펙 3종(meta_search_cases_spec.xlsx, db_validation_period_cases_spec.xlsx,
join_missing_key_spec.xlsx)이 각각 의도한 분기를 실제로 타는지 자동 승인 confirm_fn으로
끝까지 돌려 검증한다. 결과를 UTF-8 텍스트 파일로 저장한다(콘솔 코드페이지 깨짐 방지).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.parsing_agent import run_parsing
from agents.meta_search_agent import run_meta_search
from agents.join_resolution_agent import run_join_resolution
from agents.db_validation_agent import run_db_validation
from agents.classification_agent import run_classification
from llm_client import embed

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "verify_new_test_specs_result.txt")

lines = []


def log(s=""):
    lines.append(str(s))


def auto_confirm(payload: dict):
    ptype = payload.get("type")
    if ptype == "table_disambiguation_confirmation":
        # dim_customer가 아닌 다른 테이블을 골라도 되는지까지 보여주기 위해 후보 중 첫 번째를 승인
        return {"decision": "approved", "selected_table_id": payload["candidates"][0]["table_id"]}
    if ptype in ("row_completion_confirmation",):
        return {"decision": "approved", "approved_row_indices": [c["row_index"] for c in payload["candidates"]]}
    return "approved"


# ── #3: meta_search_cases_spec.xlsx ─────────────────────────
log("=" * 70)
log("[#3] meta_search_cases_spec.xlsx")
log("=" * 70)
path3 = os.path.join(DATA_DIR, "meta_search_cases_spec.xlsx")
parsed3 = run_parsing(path3, confirm_fn=auto_confirm)
meta3 = run_meta_search(parsed3["parsed_rows"], embed, confirm_fn=auto_confirm)
for r in meta3:
    log(f"  {r.get('영문명'):25s} -> match_status={r['match_status']:16s} "
        f"table={r['meta_row']['table_id'] if r.get('meta_row') else None}")
    log(f"      evidence: {r.get('match_evidence')}")

expected3 = {
    "mobile_number": "matched",       # ambiguous -> 담당자가 선택 -> matched
    "total_recharge_amt": None,       # auto_confirmed 또는 inferred_confirmed(human_confirm 경유) 둘 다 정상
    "satellite_uptime_ratio": "unresolved",
}
log("\n  [검증]")
by_name = {r["영문명"]: r for r in meta3}
log(f"    mobile_number ambiguous->matched: {by_name['mobile_number']['match_status'] == 'matched'}")
log(f"    total_recharge_amt LLM 경로 탐: {by_name['total_recharge_amt']['match_status'] in ('auto_confirmed', 'inferred_confirmed')} "
    f"(실제: {by_name['total_recharge_amt']['match_status']})")
log(f"    satellite_uptime_ratio no_match: {by_name['satellite_uptime_ratio']['match_status'] == 'unresolved'}")

# ── #4: db_validation_period_cases_spec.xlsx ────────────────
log("\n" + "=" * 70)
log("[#4] db_validation_period_cases_spec.xlsx")
log("=" * 70)
path4 = os.path.join(DATA_DIR, "db_validation_period_cases_spec.xlsx")
parsed4 = run_parsing(path4, confirm_fn=auto_confirm)
meta4 = run_meta_search(parsed4["parsed_rows"], embed, confirm_fn=auto_confirm)
validation4 = run_db_validation(meta4, confirm_fn=auto_confirm)
classified4 = run_classification(validation4)
for r in classified4:
    log(f"  {r.get('영문명'):20s} -> final_tag={r['final_tag']:16s} "
        f"spec_type={r.get('spec_type')} actual_type={r.get('actual_type')}")

expected4 = {
    "legacy_churn_flag": "not_found",
    "onnet_mou": "full_period",
    "total_rech_data": "confirm_period",
    "vol_3g_mb": "period_mismatch",
    "total_ic_mou": "period_mismatch",
}
by_name4 = {r["영문명"]: r for r in classified4}
log("\n  [검증]")
all_ok = True
for name, expected_tag in expected4.items():
    actual_tag = by_name4[name]["final_tag"]
    ok = actual_tag == expected_tag
    all_ok = all_ok and ok
    log(f"    {name:20s} 기대={expected_tag:16s} 실제={actual_tag:16s} {'OK' if ok else 'MISMATCH'}")
log(f"  전체 일치: {all_ok}")

# ── #5: join_missing_key_spec.xlsx ──────────────────────────
log("\n" + "=" * 70)
log("[#5] join_missing_key_spec.xlsx")
log("=" * 70)
path5 = os.path.join(DATA_DIR, "join_missing_key_spec.xlsx")
parsed5 = run_parsing(path5, confirm_fn=auto_confirm)
meta5 = run_meta_search(parsed5["parsed_rows"], embed, confirm_fn=auto_confirm)

hitl_seen = []


def tracking_confirm(payload):
    if payload.get("type") == "missing_join_key_confirmation":
        hitl_seen.append(payload)
    return auto_confirm(payload)


join_result5 = run_join_resolution(meta5, confirm_fn=tracking_confirm)
log(f"  조인 쌍: {len(join_result5['join_results'])}건, "
    f"joinable={sum(1 for r in join_result5['join_results'] if r['status'] == 'resolved')}")
log(f"  missing_join_key_confirmation 호출 횟수: {len(hitl_seen)} (기대: 1)")
added5 = [m for m in join_result5["meta_results"] if m.get("resolution_path") == "join_key_added"]
log(f"  자동 추가된 컬럼: {[(m['영문명'], m['meta_row']['table_id']) for m in added5]}")

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"결과 저장: {OUT_PATH}")
