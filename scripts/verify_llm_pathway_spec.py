"""
scripts/verify_llm_pathway_spec.py

data/llm_pathway_spec.xlsx가 실제로 Parsing Agent / Meta Search Agent의 LLM 경로를
전부 타는지 검증한다(자동 승인 confirm_fn을 주입해 콘솔 input() 없이 끝까지 실행).
결과를 UTF-8 텍스트 파일로 저장한다(콘솔 코드페이지 깨짐 방지).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.parsing_agent import run_parsing
from agents.meta_search_agent import run_meta_search
from llm_client import embed

FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "llm_pathway_spec.xlsx")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "verify_llm_pathway_result.txt")


def auto_confirm(payload: dict):
    """모든 HITL 확인을 자동 승인 (LLM이 실패했을 때만 사람 확인으로 넘어오므로,
    여기까지 오면 그대로 승인해 파이프라인이 끝까지 흐르게 한다)."""
    ptype = payload.get("type")
    if ptype == "header_row_confirmation":
        previews = payload.get("row_previews", [])
        return {"decision": "approved", "selected_row_idx": previews[1]["row_idx"] if len(previews) > 1 else 0}
    if ptype == "header_mapping_confirmation":
        suggested = payload.get("suggested_column")
        if suggested:
            return {"decision": "approved", "selected_column": suggested}
        return {"decision": "rejected"}
    if ptype == "row_completion_confirmation":
        return {"decision": "approved", "approved_row_indices": [c["row_index"] for c in payload["candidates"]]}
    if ptype == "inferred_confirmation":
        return "approved"
    return {"decision": "rejected"}


lines = []


def log(s=""):
    lines.append(str(s))


log("=" * 70)
log("[1] run_parsing 결과")
log("=" * 70)
parsed = run_parsing(FILE_PATH, confirm_fn=auto_confirm)

log(f"header_mapping_status = {parsed['header_mapping_status']}")
log(f"header_mapping_source = {parsed['header_mapping_source']}")
log(f"unresolved_header_fields = {parsed['unresolved_header_fields']}")
log(f"llm_mapping_confidence = {parsed['llm_mapping_confidence']}")
log(f"llm_mapping_evidence = {parsed['llm_mapping_evidence']}")
log(f"total_count = {parsed['total_count']}, parsed_rows = {len(parsed['parsed_rows'])}, "
    f"failed_rows = {len(parsed['failed_rows'])}")

log("\n[trace_log tool_calls] (parsing agent 내부에서 실제로 호출된 tool/LLM 목록)")
for call in parsed.get("trace_log", {}).get("tool_calls", []):
    log(f"  - {call['tool']:35s} model={call.get('model')!s:20s} ok={call['ok']} {call['duration_sec']}s")

log("\n[parsed_rows 상세]")
for r in parsed["parsed_rows"]:
    log(f"  {r}")

log("\n" + "=" * 70)
log("[2] run_meta_search 결과")
log("=" * 70)
meta_results = run_meta_search(parsed["parsed_rows"], embed, confirm_fn=auto_confirm)

for r in meta_results:
    log(f"\n영문명={r.get('영문명')}")
    log(f"  match_status = {r['match_status']}")
    log(f"  match_evidence = {r.get('match_evidence')}")
    log(f"  llm_confidence = {r.get('llm_confidence')}")
    log(f"  llm_evidence = {r.get('llm_evidence')}")
    if r.get("meta_row"):
        log(f"  matched_column = {r['meta_row']['table_id']}.{r['meta_row']['column_name']}")

log("\n" + "=" * 70)
log("요약: 어떤 LLM 경로가 실제로 호출되었는지")
log("=" * 70)
tool_names = {c["tool"] for c in parsed.get("trace_log", {}).get("tool_calls", [])}
log(f"select_header_row_with_llm 호출됨       : {'select_header_row_with_llm' in tool_names}")
log(f"generate_header_mapping_judgment 호출됨  : {'generate_header_mapping_judgment' in tool_names}")
log(f"infer_name_fields_with_llm 호출됨        : {'infer_name_fields_with_llm' in tool_names}")
llm_rows = [r for r in meta_results if r.get("llm_evidence")]
log(f"meta_search generate_match_judgment 호출된 행 수 : {len(llm_rows)} / {len(meta_results)}")

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"결과 저장: {OUT_PATH}")
