"""
run_threshold_sweep.py (RAG 전환 반영판)

B1 비교표를 실측치로 채우기 위한 스크립트. 평가셋(evaluation/eval_set.csv)에 대해
1) exact match / fuzzy match / vss 유사도 검색의 커버율을 각각 측정하고
2) LLM 판단(generate_match_judgment)까지 포함한 retry/human_confirm 분포와
   컬럼당 평균 처리시간을 측정해 evaluation/matching_comparison.csv 로 저장한다.
   (추정 매칭은 confidence 무관 항상 담당자 확인을 거치므로 auto_confirm 버킷은 없다)

평가셋 형식 (evaluation/eval_set.csv):
    영문명, 한글명, 항목설명, expected_tag
    SUBS_LINE_CNT, 가입회선수, 고객이 보유한 회선 수, matched

실행:
    python evaluation/run_threshold_sweep.py --eval-file evaluation/eval_set.csv
"""

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

from agents.meta_search_agent import (
    META_DB_PATH,
    exact_match_meta_db,
    fuzzy_match_candidates,
    retrieve_candidates,
    generate_match_judgment,
    decide_route,
    _fetch_all_columns,
)
from llm_client import embed, chat_parsed

OUTPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "matching_comparison.csv")


def load_eval_set(path: str) -> list:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def measure_exact_and_fuzzy(con, eval_rows: list) -> dict:
    all_columns = _fetch_all_columns(con)
    exact_hits, fuzzy_hits, exact_time, fuzzy_time = 0, 0, 0.0, 0.0

    for row in eval_rows:
        eng_name = row["영문명"].strip()

        t0 = time.time()
        exact = exact_match_meta_db(con, eng_name)
        exact_time += time.time() - t0
        if exact["found"]:
            exact_hits += 1
            continue

        t0 = time.time()
        fuzzy = fuzzy_match_candidates(eng_name, all_columns, top_k=3)
        fuzzy_time += time.time() - t0
        if fuzzy:
            fuzzy_hits += 1

    n = len(eval_rows)
    return {
        "exact_hit_rate": round(exact_hits / n, 4) if n else None,
        "exact_avg_time_sec": round(exact_time / n, 5) if n else None,
        "fuzzy_additional_hit_rate": round(fuzzy_hits / n, 4) if n else None,
        "fuzzy_avg_time_sec": round(fuzzy_time / n, 5) if n else None,
    }


def measure_rag_judgment(con, eval_rows: list) -> dict:
    """exact match 실패 건에 한해 retrieve_candidates + generate_match_judgment 실측.
    추정 매칭은 confidence 무관 항상 담당자 확인을 거치므로(자동 확정 경로 없음),
    route는 retry/human_confirm 둘 중 하나다."""
    counts = {"retry": 0, "human_confirm": 0, "no_candidates": 0}
    elapsed_total = 0.0
    evaluated = 0

    for row in eval_rows:
        eng_name = row["영문명"].strip()
        exact = exact_match_meta_db(con, eng_name)
        if exact["found"]:
            continue

        evaluated += 1
        t0 = time.time()
        candidates = retrieve_candidates(con, eng_name, row["항목설명"], embed, top_k=5)
        if not candidates:
            counts["no_candidates"] += 1
            elapsed_total += time.time() - t0
            continue

        judgment = generate_match_judgment(row, candidates, chat_fn=chat_parsed)
        route = decide_route(judgment, retrieval_attempts=0)
        counts[route if route in counts else "human_confirm"] += 1
        elapsed_total += time.time() - t0

    return {
        "rag_evaluated_count": evaluated,
        "rag_retry": counts["retry"],
        "rag_human_confirm": counts["human_confirm"],
        "rag_no_candidates": counts["no_candidates"],
        "rag_avg_time_sec": round(elapsed_total / evaluated, 4) if evaluated else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-file", default="evaluation/eval_set.csv")
    args = parser.parse_args()

    eval_rows = load_eval_set(args.eval_file)
    print(f"[평가셋] {args.eval_file} - {len(eval_rows)}건 로드")

    con = duckdb.connect(META_DB_PATH)
    con.execute("LOAD vss;")

    exact_fuzzy_stats = measure_exact_and_fuzzy(con, eval_rows)
    rag_stats = measure_rag_judgment(con, eval_rows)
    con.close()

    combined = {**exact_fuzzy_stats, **rag_stats}

    print("\n[측정 결과]")
    for k, v in combined.items():
        print(f"  {k}: {v}")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(combined.keys()))
        writer.writeheader()
        writer.writerow(combined)
    print(f"\n[저장 완료] {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
