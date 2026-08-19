"""
[로컬 실행 전용] column_spec 설명 임베딩 생성 + column_embeddings 적재 + HNSW 인덱스

사전 조건:
- test_setup.py를 먼저 실행해 메타 DB에 vss 확장이 설치되어 있어야 함
- 가능하면 llm_polish_columns.py를 먼저 실행해 description을 자연스럽게 다듬은 뒤 사용 권장
  (다듬지 않은 규칙 기반 설명으로도 동작은 함)

실행: python generate_embeddings.py
"""

import os
import sys
from pathlib import Path

# llm_client.py가 프로젝트 루트에 있으므로, scripts/ 안에서 실행해도 찾을 수 있게 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
from dotenv import load_dotenv
from llm_client import embed

load_dotenv()

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")
BATCH_SIZE = 50  # 임베딩 API 한 번에 보낼 텍스트 수


def main():
    con = duckdb.connect(META_DB_PATH)
    con.execute("LOAD vss;")
    con.execute("SET hnsw_enable_experimental_persistence = true;")

    rows = con.execute(
        "SELECT column_id, description FROM column_spec ORDER BY column_id"
    ).fetchall()
    print(f"[INFO] 임베딩 대상 컬럼 수: {len(rows)}")

    con.execute("DELETE FROM column_embeddings")

    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        texts = [desc for _, desc in batch]
        column_ids = [cid for cid, _ in batch]

        resp = embed("DEPLOYMENT_EMBED_LARGE", texts)
        vectors = [d.embedding for d in resp.data]

        con.executemany(
            "INSERT INTO column_embeddings (column_id, embedding) VALUES (?, ?)",
            list(zip(column_ids, vectors)),
        )
        inserted += len(batch)
        print(f"[embed] {inserted}/{len(rows)} 처리 완료")

    # HNSW 인덱스 생성 (이미 있으면 재생성)
    con.execute("DROP INDEX IF EXISTS idx_column_embedding")
    con.execute(
        "CREATE INDEX idx_column_embedding ON column_embeddings USING HNSW (embedding) WITH (metric = 'cosine')"
    )

    n = con.execute("SELECT COUNT(*) FROM column_embeddings").fetchone()[0]
    print(f"\n[DONE] column_embeddings {n}건 적재 + HNSW 인덱스 생성 완료")

    con.close()


if __name__ == "__main__":
    main()
