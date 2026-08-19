"""
메타 DB(schemascout_meta.duckdb) 최종 검증

체크 항목:
1. vss 확장이 로드되어 있는가
2. table_catalog: 6개 테이블 등록 여부
3. column_spec: 74개 컬럼, data_type/description 채워짐 여부, 의도적 type_mismatch 5건 확인
4. column_embeddings: 74건 적재 + HNSW 인덱스 존재 여부 + 실제 유사도 검색 동작 테스트
5. table_relationships: 5건(dim_customer ↔ 각 fact) 등록 여부

실행: python scripts/verify_meta_db.py
"""

import os
import duckdb
from dotenv import load_dotenv

load_dotenv()

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")

# fill_meta_db.py에서 의도적으로 넣은 type_mismatch 케이스 (교차검증용)
EXPECTED_MISMATCHES = {
    "raw_telecom_dim_customer.churn_month": "VARCHAR",
    "raw_telecom_fact_call_outgoing.onnet_mou": "INTEGER",
    "raw_telecom_fact_data_usage.vol_3g_mb": "INTEGER",
    "raw_telecom_fact_recharge_data.total_rech_data": "DOUBLE",
    "raw_telecom_fact_recharge_voice.total_rech_amt": "VARCHAR",
}


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    return condition


def main():
    con = duckdb.connect(META_DB_PATH, read_only=True)
    all_ok = True

    print("=" * 60)
    print("1. vss 확장 로드 확인")
    print("=" * 60)
    try:
        con.execute("LOAD vss;")
        all_ok &= check("vss 확장 로드", True)
    except Exception as e:
        all_ok &= check("vss 확장 로드", False, str(e))

    print("\n" + "=" * 60)
    print("2. table_catalog")
    print("=" * 60)
    n_tables = con.execute("SELECT COUNT(*) FROM table_catalog").fetchone()[0]
    all_ok &= check("테이블 6개 등록", n_tables == 6, f"실제 {n_tables}개")
    for row in con.execute("SELECT table_name, row_count FROM table_catalog ORDER BY table_name").fetchall():
        print(f"   - {row[0]}: {row[1]:,} rows")

    print("\n" + "=" * 60)
    print("3. column_spec")
    print("=" * 60)
    n_cols = con.execute("SELECT COUNT(*) FROM column_spec").fetchone()[0]
    all_ok &= check("컬럼 74개 등록", n_cols == 74, f"실제 {n_cols}개")

    n_null_desc = con.execute(
        "SELECT COUNT(*) FROM column_spec WHERE description IS NULL OR description = ''"
    ).fetchone()[0]
    all_ok &= check("description 빈 값 없음", n_null_desc == 0, f"빈 값 {n_null_desc}개")

    n_null_type = con.execute(
        "SELECT COUNT(*) FROM column_spec WHERE data_type IS NULL"
    ).fetchone()[0]
    all_ok &= check("data_type 빈 값 없음", n_null_type == 0, f"빈 값 {n_null_type}개")

    print("\n   [의도적 type_mismatch 5건 교차검증]")
    for column_id, expected_type in EXPECTED_MISMATCHES.items():
        row = con.execute(
            "SELECT data_type FROM column_spec WHERE column_id = ?", [column_id]
        ).fetchone()
        ok = row is not None and row[0] == expected_type
        all_ok &= check(f"   {column_id}", ok, f"기대={expected_type}, 실제={row[0] if row else 'NOT FOUND'}")

    print("\n   [description 다듬기 전/후 샘플 비교]")
    sample = con.execute(
        "SELECT column_id, description FROM column_spec ORDER BY RANDOM() LIMIT 3"
    ).fetchall()
    for col_id, desc in sample:
        polished = "|" not in desc  # llm_polish 이후엔 "한글명 | 설명" 구분자가 사라짐
        print(f"   - {col_id}: {desc}  {'(다듬어짐)' if polished else '(미다듬음 - 규칙조합 원본)'}")

    print("\n" + "=" * 60)
    print("4. column_embeddings")
    print("=" * 60)
    n_emb = con.execute("SELECT COUNT(*) FROM column_embeddings").fetchone()[0]
    all_ok &= check("임베딩 74건 적재", n_emb == 74, f"실제 {n_emb}건")

    idx_exists = con.execute(
        "SELECT COUNT(*) FROM duckdb_indexes() WHERE index_name = 'idx_column_embedding'"
    ).fetchone()[0]
    all_ok &= check("HNSW 인덱스(idx_column_embedding) 존재", idx_exists > 0)

    if n_emb > 0:
        try:
            sample_vec = con.execute("SELECT embedding FROM column_embeddings LIMIT 1").fetchone()[0]
            similar = con.execute(
                """
                SELECT cs.column_id, array_cosine_distance(ce.embedding, ?::FLOAT[3072]) AS dist
                FROM column_embeddings ce
                JOIN column_spec cs ON ce.column_id = cs.column_id
                ORDER BY dist ASC
                LIMIT 3
                """,
                [sample_vec],
            ).fetchall()
            all_ok &= check("유사도 검색 실제 동작", len(similar) == 3)
            print("   상위 3건(자기 자신이 1위, distance=0에 가까워야 정상):")
            for col_id, dist in similar:
                print(f"     {col_id}: {dist:.4f}")
        except Exception as e:
            all_ok &= check("유사도 검색 실제 동작", False, str(e))

    print("\n" + "=" * 60)
    print("5. table_relationships")
    print("=" * 60)
    n_rel = con.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0]
    all_ok &= check("관계 5건 등록", n_rel == 5, f"실제 {n_rel}건")
    for row in con.execute(
        "SELECT from_table_id, to_table_id, join_key, relation_type FROM table_relationships"
    ).fetchall():
        print(f"   - {row[0]} → {row[1]} ({row[2]}, {row[3]})")

    print("\n" + "=" * 60)
    print("최종 결과:", "✅ 전체 통과" if all_ok else "❌ 일부 실패 - 위 FAIL 항목 확인 필요")
    print("=" * 60)

    con.close()


if __name__ == "__main__":
    main()