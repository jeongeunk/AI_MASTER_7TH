"""
scripts/verify_full_pipeline_with_join.py

data/llm_pathway_spec.xlsx(dim_customer + 2개 fact 테이블에 걸친 컬럼 요청)를
Parsing -> Meta Search -> Join Resolution -> DB Validation -> Classification -> Report
전체로 돌려서, 리포트에 "조인 가능성 검증" 시트가 실제로 만들어지는지 확인한다.
콘솔 input() 없이 끝까지 흐르도록 모든 HITL을 자동 승인한다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from agents.parsing_agent import run_parsing
from agents.meta_search_agent import run_meta_search
from agents.join_resolution_agent import run_join_resolution
from agents.db_validation_agent import run_db_validation
from agents.classification_agent import run_classification
from agents.report_agent import run_report
from llm_client import embed

FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "llm_pathway_spec.xlsx")


def auto_confirm(payload: dict):
    ptype = payload.get("type")
    if ptype == "header_row_confirmation":
        previews = payload.get("row_previews", [])
        return {"decision": "approved", "selected_row_idx": previews[1]["row_idx"] if len(previews) > 1 else 0}
    if ptype == "header_mapping_confirmation":
        suggested = payload.get("suggested_column")
        return {"decision": "approved", "selected_column": suggested} if suggested else {"decision": "rejected"}
    if ptype == "row_completion_confirmation":
        return {"decision": "approved", "approved_row_indices": [c["row_index"] for c in payload["candidates"]]}
    return "approved"


parsed = run_parsing(FILE_PATH, confirm_fn=auto_confirm)
meta_results = run_meta_search(parsed["parsed_rows"], embed, confirm_fn=auto_confirm)
join_result = run_join_resolution(meta_results, confirm_fn=auto_confirm)
join_results = join_result["join_results"]
meta_results = join_result["meta_results"]  # 조인키가 자동 추가됐을 수 있으므로 갱신본 사용
validation_results = run_db_validation(meta_results, confirm_fn=auto_confirm)
classified = run_classification(validation_results)

result = run_report(meta_results, classified, join_results=join_results, input_file_path=FILE_PATH)

print(f"엑셀 산출물: {result['excel_path']}")
print(f"stats: {result['stats']}")

added = [m for m in meta_results if m.get("resolution_path") == "join_key_added"]
print(f"자동 추가된 조인키 컬럼: {[(m['영문명'], m['meta_row']['table_id']) for m in added]}")

xls = pd.ExcelFile(result["excel_path"])
print(f"\n시트 목록: {xls.sheet_names}")
if "조인 가능성 검증" in xls.sheet_names:
    join_df = pd.read_excel(result["excel_path"], sheet_name="조인 가능성 검증")
    print("\n[조인 가능성 검증 시트 내용]")
    print(join_df.to_string())
else:
    print("\n[경고] '조인 가능성 검증' 시트가 생성되지 않았습니다")
