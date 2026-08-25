"""
scripts/verify_join_resolution.py

join_resolution_agent가 세 가지 경로를 실제로 다 타는지 검증한다:
1. table_relationships에 이미 등록된 직접 관계 (dim_customer <-> fact_data_usage)
2. 등록된 관계가 없어도 dim_customer를 경유하는 2-hop 경로
   (fact_call_incoming <-> fact_data_usage, 둘 다 fact-fact라 직접 관계는 없음)
3. 아예 관계 그래프가 비어있다고 가정했을 때의 "추정" 경로 (규칙 기반 후보 생성 ->
   값 overlap 실측 -> HITL 확인 -> table_relationships 저장 -> type/기간 검증)
   fact_call_incoming <-> fact_recharge_voice 쌍으로, edges=[]를 강제 주입해 테스트.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.db_validation_agent import get_guarded_connection
from agents.join_resolution_agent import (
    find_join_path,
    resolve_join_for_pair,
    run_join_resolution,
    _load_relationship_edges,
)

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "verify_join_resolution_result.txt")
lines = []


def log(s=""):
    lines.append(str(s))


def auto_confirm(payload):
    return "approved"


con = get_guarded_connection()
con.execute("LOAD vss;")
edges = _load_relationship_edges(con)

log("=" * 70)
log("[1] 기존 등록된 직접 관계: dim_customer <-> fact_data_usage")
log("=" * 70)
r1 = resolve_join_for_pair(con, "raw_telecom_dim_customer", "raw_telecom_fact_data_usage", edges)
for k, v in r1.items():
    log(f"  {k}: {v}")

log("\n" + "=" * 70)
log("[2] 직접 관계 없음 -> dim_customer 경유 2-hop: fact_call_incoming <-> fact_data_usage")
log("=" * 70)
path = find_join_path(edges, "raw_telecom_fact_call_incoming", "raw_telecom_fact_data_usage")
log(f"  BFS 경로: {path}")
r2 = resolve_join_for_pair(con, "raw_telecom_fact_call_incoming", "raw_telecom_fact_data_usage", edges)
for k, v in r2.items():
    log(f"  {k}: {v}")

log("\n" + "=" * 70)
log("[3] 관계 그래프 비어있다고 가정(edges=[]) -> 추정 경로 강제 트리거: "
    "fact_call_incoming <-> fact_recharge_voice")
log("=" * 70)
r3 = resolve_join_for_pair(con, "raw_telecom_fact_call_incoming", "raw_telecom_fact_recharge_voice",
                            edges=[], confirm_fn=auto_confirm)
for k, v in r3.items():
    log(f"  {k}: {v}")

# 방금 저장된 inferred_confirmed 관계가 실제로 table_relationships에 들어갔는지, 그리고
# 재실행 시 이제는 "declared"(재확인 없이 재사용)로 처리되는지 확인
log("\n  [3-1] 저장 확인: table_relationships에 방금 생긴 행")
saved = con.execute(
    "SELECT relationship_id, from_table_id, to_table_id, join_key, source, confidence "
    "FROM table_relationships WHERE source = 'inferred_confirmed'"
).fetchall()
for row in saved:
    log(f"    {row}")

log("\n  [3-2] 같은 쌍을 실제 edges(방금 저장분 포함)로 다시 조회하면 declared로 처리되는지")
edges_after = _load_relationship_edges(con)
r3b = resolve_join_for_pair(con, "raw_telecom_fact_call_incoming", "raw_telecom_fact_recharge_voice",
                             edges=edges_after, confirm_fn=auto_confirm)
log(f"    source = {r3b['source']} (기대: declared, HITL 없이 바로 재사용)")

con.close()

log("\n" + "=" * 70)
log("[4] run_join_resolution 전체 오케스트레이션 (mobile_number도 요청 컬럼에 포함된 경우)")
log("=" * 70)
fake_meta_results = [
    {"match_status": "matched", "meta_row": {"table_id": "raw_telecom_dim_customer", "column_name": "mobile_number"}},
    {"match_status": "matched", "meta_row": {"table_id": "raw_telecom_fact_call_incoming", "column_name": "total_ic_mou"}},
    {"match_status": "matched", "meta_row": {"table_id": "raw_telecom_fact_data_usage", "column_name": "vol_3g_mb"}},
]
result = run_join_resolution(fake_meta_results, confirm_fn=auto_confirm)
join_results = result["join_results"]
log(f"  검증된 테이블 쌍 수: {len(join_results)}")
for r in join_results:
    log(f"  {r['table_a']} <-> {r['table_b']} : status={r['status']} source={r.get('source')} "
        f"joined_period={r.get('joined_period')} type_consistent={r.get('type_consistent')}")
added = [m for m in result["meta_results"] if m.get("resolution_path") == "join_key_added"]
log(f"  자동 추가된 키 컬럼 수: {len(added)} (기대: 0 - mobile_number가 이미 요청 목록에 있음)")

log("\n" + "=" * 70)
log("[5] 조인키가 요청 목록에 없는 경우 (mobile_number 미포함) -> 누락 알림 + HITL + 자동 추가")
log("=" * 70)
fake_meta_results_no_key = [
    {"match_status": "matched", "meta_row": {"table_id": "raw_telecom_dim_customer", "column_name": "circle_id"}},
    {"match_status": "matched", "meta_row": {"table_id": "raw_telecom_fact_call_incoming", "column_name": "total_ic_mou"}},
    {"match_status": "matched", "meta_row": {"table_id": "raw_telecom_fact_data_usage", "column_name": "vol_3g_mb"}},
]

hitl_calls = []


def tracking_confirm(payload):
    hitl_calls.append(payload)
    return "approved"


result2 = run_join_resolution(fake_meta_results_no_key, confirm_fn=tracking_confirm)
missing_key_prompts = [p for p in hitl_calls if p.get("type") == "missing_join_key_confirmation"]
log(f"  missing_join_key_confirmation 호출 횟수: {len(missing_key_prompts)} (기대: 1 - mobile_number 한 번만 물어봄)")
for p in missing_key_prompts:
    log(f"    key_column={p['key_column']} tables={p['tables']} needed_for={p['needed_for']}")
    log(f"    근거: {p['evidence']}")

added2 = [m for m in result2["meta_results"] if m.get("resolution_path") == "join_key_added"]
log(f"  자동 추가된 키 컬럼: {[(m['영문명'], m['meta_row']['table_id']) for m in added2]}")
log(f"  추가된 행의 한글명 표시: {[m['한글명'] for m in added2]}")

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"결과 저장: {OUT_PATH}")
