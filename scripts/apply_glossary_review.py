"""
[로컬 실행 전용] 담당자 검수 결과를 glossary_terms에 반영 (3/3단계)

scripts/llm_expand_glossary.py가 만든 glossary_review_candidates.csv에서
담당자가 approve 칸을 'y'로 채운 행만 glossary_terms에 반영한다.
반영된 행은 source='llm_generated_reviewed'로 갱신되어, 이후
scripts/generate_glossary_embeddings.py가 임베딩 대상으로 인식한다.

실행: python scripts/apply_glossary_review.py [csv_path]
      (csv_path 생략 시 scripts/glossary_review_candidates.csv 사용)
"""

import os
import sys
import csv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")
DEFAULT_CSV = str(Path(__file__).resolve().parent / "glossary_review_candidates.csv")


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    con = duckdb.connect(META_DB_PATH)

    approved, skipped = 0, 0
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if str(row.get("approve", "")).strip().lower() != "y":
                skipped += 1
                continue
            con.execute(
                "UPDATE glossary_terms SET synonyms = ?, domain = ?, source = 'llm_generated_reviewed', "
                "updated_at = current_timestamp WHERE term_id = ?",
                [row["llm_suggested_synonyms"], row.get("domain") or None, row["term_id"]],
            )
            approved += 1

    con.close()
    print(f"[apply_glossary_review] 반영 {approved}건, 미승인/스킵 {skipped}건")
    print("다음 단계: python scripts/generate_glossary_embeddings.py")


if __name__ == "__main__":
    main()
