"""
evaluation/run_reproducibility_check.py

"판정 재현성(Reproducibility)" KPI 측정 스크립트
(산출물/문제 정의 및 서비스 기획_0827.md의 정량적 목표 KPI 표 참고).

동일 명세서를 N회 반복 실행해, 추정 매칭 컬럼(정확 매칭은 구조상 항상 100%
재현되므로 집계에서 제외)의 최종 column_id가 매번 동일하게 나오는지 측정한다.

주의: 이 스크립트는 confirmed_mapping_examples(Episodic Memory)에 실제로 적재하며
실행한다 - persist_confirmed_mapping_example이 (column_id, eng_name) 기준으로 멱등
처리되어 있어 반복 실행해도 중복 적재는 되지 않지만, 첫 실행에서 확정된 매핑이
이후 실행의 후보 검색(confirmed_mapping 소스)에 영향을 줄 수 있다는 점은 감안해야
한다 - 이는 실제 운영 환경과 동일한 조건이므로 오히려 현실적인 측정치다.

실행:
    python evaluation/run_reproducibility_check.py --file data/llm_pathway_spec.xlsx --repeat 5
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.parsing_agent import run_parsing
from agents.meta_search_agent import run_meta_search
from llm_client import embed


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
    if ptype == "table_disambiguation_confirmation":
        return {"decision": "approved", "selected_table_id": payload["candidates"][0]["table_id"]}
    return "approved"


def run_once(file_path: str) -> dict:
    """{eng_name: (match_status, column_id)} 반환"""
    parsed = run_parsing(file_path, confirm_fn=auto_confirm)
    results = run_meta_search(parsed["parsed_rows"], embed, confirm_fn=auto_confirm)
    out = {}
    for r in results:
        eng_name = str(r.get("영문명") or "").strip()
        if not eng_name:
            continue
        column_id = r["meta_row"]["column_id"] if r.get("meta_row") else None
        out[eng_name] = (r["match_status"], column_id)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()

    runs = []
    for i in range(args.repeat):
        print(f"[{i + 1}/{args.repeat}] 실행 중...")
        runs.append(run_once(args.file))

    all_eng_names = set()
    for run in runs:
        all_eng_names.update(run.keys())

    print("\n[재현성 결과] (정확 매칭만 나온 컬럼은 구조상 항상 100%이므로 집계에서 제외)")
    stable, unstable = 0, 0
    for eng_name in sorted(all_eng_names):
        outcomes = [run.get(eng_name) for run in runs]
        statuses = {o[0] for o in outcomes if o is not None}
        if statuses == {"matched"}:
            continue

        column_ids = [o[1] for o in outcomes if o is not None]
        agreement = Counter(column_ids).most_common(1)[0][1] / len(column_ids) if column_ids else 0.0
        if agreement == 1.0:
            stable += 1
        else:
            unstable += 1
            print(f"  [불일치] {eng_name}: {outcomes}")

    total = stable + unstable
    rate = round(stable / total * 100, 1) if total else None
    print(f"\n측정 대상 컬럼(추정 매칭) {total}건 중 {stable}건 매번 동일한 column_id -> 재현율 {rate}%")


if __name__ == "__main__":
    main()
