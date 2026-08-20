"""
[로컬 실행 전용] glossary_embeddings 생성 + HNSW 인덱스

source='llm_generated_reviewed' 또는 'manual'로 확정된 glossary_terms만 임베딩 대상으로
삼는다(source='draft'인 미검수 항목은 제외 — 검수 없이 임베딩·검색에 노출되지 않도록 방어).
generate_embeddings.py(column_embeddings용)와 동일한 패턴을 따른다.

실행 전 확인:
- .env에 DEPLOYMENT_EMBED_LARGE가 설정되어 있어야 함
- scripts/apply_glossary_review.py로 검수가 완료된 상태여야 함

실행: python scripts/generate_glossary_embeddings.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
from llm_client import embed

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")


def main():
    con = duckdb.connect(META_DB_PATH)
    con.execute("INSTALL vss; LOAD vss;")
    con.execute("SET hnsw_enable_experimental_persistence = true;")

    con.execute("""
        CREATE TABLE IF NOT EXISTS glossary_embeddings (
            term_id VARCHAR,
            embedding FLOAT[3072]
        )
    """)
    con.execute("DELETE FROM glossary_embeddings")  # 재실행 시 최신 검수 결과로 전량 재생성

    rows = con.execute(
        "SELECT term_id, canonical_term, synonyms, description FROM glossary_terms "
        "WHERE source IN ('llm_generated_reviewed', 'manual')"
    ).fetchall()

    print(f"[generate_glossary_embeddings] 임베딩 대상 {len(rows)}건")

    for term_id, canonical_term, synonyms, description in rows:
        combined_text = f"{canonical_term} {synonyms or ''} {description or ''}".strip()
        resp = embed("DEPLOYMENT_EMBED_LARGE", combined_text)
        vec = resp.data[0].embedding
        con.execute("INSERT INTO glossary_embeddings (term_id, embedding) VALUES (?, ?)", [term_id, vec])

    con.execute("""
        CREATE INDEX IF NOT EXISTS glossary_embeddings_hnsw
        ON glossary_embeddings USING HNSW (embedding)
        WITH (metric = 'cosine')
    """)

    con.close()
    print("[generate_glossary_embeddings] 완료 (HNSW cosine 인덱스 생성됨)")


if __name__ == "__main__":
    main()
