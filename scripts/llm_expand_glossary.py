"""
[로컬 실행 전용] glossary_terms LLM 보강 (2/3단계)

build_glossary_seed.py로 만든 초안(source='draft')을 gpt-4.1-mini에 배치 호출해
통신 도메인 지식 기반 동의어 후보 3~5개씩 추가 생성한다.
LLM 결과는 바로 DB에 반영하지 않고 CSV로 내보내 담당자 검수를 거치게 한다
(3단계: 담당자가 CSV를 확인하고 scripts/apply_glossary_review.py로 반영).

실행 전 확인:
- .env에 LLM_BASE_URL / LLM_API_KEY / DEPLOYMENT_GPT41_MINI가 설정되어 있어야 함
- scripts/build_glossary_seed.py를 먼저 실행해 glossary_terms(source='draft')가 있어야 함

실행: python scripts/llm_expand_glossary.py
출력: scripts/glossary_review_candidates.csv (담당자 검수용)
"""

import os
import sys
import json
import csv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
from llm_client import chat

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")
BATCH_SIZE = 15
OUTPUT_CSV = str(Path(__file__).resolve().parent / "glossary_review_candidates.csv")

SYSTEM_PROMPT = """너는 통신사 데이터 명세서의 도메인 용어집을 만드는 어시스턴트야.
입력으로 (term_id, canonical_term, 기존 synonyms, description) 목록을 JSON 배열로 받는다.
각 항목에 대해 통신 도메인에서 통용되는 동의어(영문 약어, 한글 줄임말, 실무 표현 등) 3~5개를
기존 synonyms에 추가로 제안해라. 확신 없는 용어는 만들어내지 마라.
출력은 반드시 다음 형식의 JSON 배열만:
[{"term_id": "...", "additional_synonyms": ["...", "..."], "domain": "가입|과금|통화|기타 중 하나"}]
다른 텍스트(설명, 코드블록 표시 등)는 절대 포함하지 마라."""


def expand_batch(rows: list) -> list:
    payload = [
        {"term_id": r[0], "canonical_term": r[1], "synonyms": r[2], "description": r[4]}
        for r in rows
    ]
    resp = chat(
        "DEPLOYMENT_GPT41_MINI",
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.3,
    )
    text = resp.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    return json.loads(text)


def main():
    con = duckdb.connect(META_DB_PATH)
    rows = con.execute(
        "SELECT term_id, canonical_term, synonyms, linked_column_id, description "
        "FROM glossary_terms WHERE source = 'draft'"
    ).fetchall()
    con.close()

    print(f"[llm_expand_glossary] {len(rows)}건 대상 LLM 보강 시작")

    review_rows = []
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        print(f"  {i+1}~{i+len(batch)} / {len(rows)} 처리 중...")
        try:
            expanded = expand_batch(batch)
        except Exception as e:
            print(f"  [WARN] 배치 실패, 건너뜀: {e}")
            continue

        by_id = {r[0]: r for r in batch}
        for item in expanded:
            term_id = item.get("term_id")
            orig = by_id.get(term_id)
            if not orig:
                continue
            merged_synonyms = ",".join(sorted(set(
                orig[2].split(",") + item.get("additional_synonyms", [])
            )))
            review_rows.append({
                "term_id": term_id,
                "canonical_term": orig[1],
                "original_synonyms": orig[2],
                "llm_suggested_synonyms": merged_synonyms,
                "domain": item.get("domain", ""),
                "linked_column_id": orig[3],
                "approve": "",  # 담당자가 y/n으로 채울 칸
            })

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(review_rows[0].keys()) if review_rows else [])
        if review_rows:
            writer.writeheader()
            writer.writerows(review_rows)

    print(f"\n[저장 완료] {OUTPUT_CSV} ({len(review_rows)}건)")
    print("담당자 검수: approve 칸에 y/n을 채운 뒤 scripts/apply_glossary_review.py 실행")


if __name__ == "__main__":
    main()
