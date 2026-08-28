"""
evaluation/run_retrieval_attempts_check.py

"컬럼 재등장 시 LLM 호출(재검색) 감소율" KPI 측정 스크립트
(산출물/문제 정의 및 서비스 기획_0827.md의 정량적 목표 KPI 표 참고).

동일 명세서를 N회 반복 실행해, 추정 매칭 컬럼(inferred_confirmed)의 retrieval_attempts가
재등장할수록 줄어드는지 측정한다. run_reproducibility_check.py와 자매 스크립트이며,
"같은 결론이 나오는가"(재현성) 대신 "같은 컬럼이 다시 나오면 계산 비용이 줄어드는가"
(효율성)를 측정한다는 점이 다르다.

주의:
- confirmed_mapping_examples(Episodic Memory)에 실제로 적재하며 실행한다 -
  첫 실행에서 담당자가 승인한 매핑이 이후 실행의 재검색 여부에 영향을 준다(현실과 동일 조건).
- 대상 컬럼은 반드시 "1회차부터 재검색(retry)이 실제로 발생하는" 컬럼이어야 의미가 있다.
  이미 confidence가 처음부터 높아 retrieval_attempts=0으로 시작하는 컬럼은 0에서 더
  줄어들 수 없어 감소율을 보여줄 수 없다(정확 매칭 컬럼과 같은 문제).

실행:
    python evaluation/run_retrieval_attempts_check.py --file "산출물/재검색 유발 테스트(0828).xlsx" --repeat 3

주의: 이 파일의 컬럼은 실행할 때마다 confirmed_mapping_examples에 캐시되므로, 한 번 돌리고 나면
그 컬럼은 더 이상 "최초 등장"이 아니게 되어 재검색이 발생하지 않는다(1회차부터 0회). 다시
1회차부터의 감소 추이를 보고 싶으면 confirmed_mapping_examples/confirmed_mapping_embeddings에서
해당 eng_name 행을 지우거나, 아직 캐시되지 않은 새 컬럼으로 테스트해야 한다.
"""

import argparse
import os
import sys

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
    return "approved"  # inferred_confirmation 등 - 항상 승인해야 다음 회차에서 캐시 효과를 관찰 가능


def run_once(file_path: str) -> dict:
    """{eng_name: (match_status, retrieval_attempts, column_id)} 반환"""
    parsed = run_parsing(file_path, confirm_fn=auto_confirm)
    results = run_meta_search(parsed["parsed_rows"], embed, confirm_fn=auto_confirm)
    out = {}
    for r in results:
        eng_name = str(r.get("영문명") or "").strip()
        if not eng_name:
            continue
        column_id = r["meta_row"]["column_id"] if r.get("meta_row") else None
        out[eng_name] = (r.get("match_status"), r.get("retrieval_attempts"), column_id)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    runs = []
    for i in range(args.repeat):
        print(f"[{i + 1}/{args.repeat}] 실행 중...")
        runs.append(run_once(args.file))

    all_eng_names = set()
    for run in runs:
        all_eng_names.update(run.keys())

    print("\n[재검색 횟수 추이] (정확 매칭 컬럼은 retrieval_attempts가 애초에 없어 집계 제외)")
    for eng_name in sorted(all_eng_names):
        trend = []
        for run in runs:
            outcome = run.get(eng_name)
            if outcome is None:
                trend.append(None)
                continue
            status, attempts, column_id = outcome
            trend.append(attempts)

        if all(a is None for a in trend):
            continue  # 정확 매칭이라 retrieval_attempts 자체가 없는 컬럼

        print(f"  {eng_name}: {trend}")
        first = trend[0]
        rest = [a for a in trend[1:] if a is not None]
        if first and rest:
            avg_rest = sum(rest) / len(rest)
            if avg_rest < first:
                reduction = round((1 - avg_rest / first) * 100, 1)
                print(f"    -> 1회차 {first}회 대비 재등장 시 평균 {avg_rest:.1f}회로 {reduction}% 감소")
            elif avg_rest == first:
                print(f"    -> 1회차와 재등장 시 재검색 횟수 동일({first}회) - 감소 없음")
            else:
                print(f"    -> 재등장 시 오히려 재검색 횟수 증가(1회차 {first}회 -> 평균 {avg_rest:.1f}회) - 원인 확인 필요")
        elif first == 0:
            print("    -> 1회차부터 이미 재검색 0회 - 이 컬럼으로는 감소율을 관찰할 수 없음(0에서 더 줄지 않음)")


if __name__ == "__main__":
    main()
