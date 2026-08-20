"""
generate_fixtures.py

repo를 새로 clone한 사람이 원본 Kaggle 데이터 없이도 바로
`python run_pipeline.py data/sample_spec.xlsx`를 실행해볼 수 있도록,
아주 작은 합성 데이터로 3개 DB(meta/data/audit)를 처음부터 생성한다.
(RAG 전환 반영: glossary_terms/glossary_embeddings, 감사 DB의 auto_confirm_log 추가)

실행:
    python scripts/generate_fixtures.py
"""

import os
import random
import sqlite3

import duckdb

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")
DATA_DB_PATH = os.environ.get("DATA_DB_PATH", "./db/schemascout_data.duckdb")
AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")

EMBED_DIM = 3072  # text-embedding-3-large 기준

SAMPLE_COLUMNS = [
    ("col_001", "raw_customer", "SUBS_LINE_CNT", "VARCHAR", "고객이 보유한 회선 수"),
    ("col_002", "raw_customer", "ARPU", "INTEGER", "가입자당 평균 매출"),
    ("col_003", "raw_call", "CALL_CNT", "INTEGER", "월별 통화 횟수"),
    ("col_004", "raw_customer", "LEGACY_FLAG", "VARCHAR", "레거시 시스템 이관 여부 플래그"),  # not_found 케이스용
    ("col_005", "raw_customer", "REGION_CD", "INTEGER", "고객 거주 지역 코드"),  # type_mismatch 케이스용
]

SAMPLE_RELATIONSHIPS = [
    ("raw_customer", "raw_call", "customer_id", "1:N"),
]

# glossary 시드: SUBS_LINE_CNT 계열 동의어 검증용
SAMPLE_GLOSSARY = [
    ("term_col_001", "가입회선수", "회선수,line count,가입회선", "가입", "고객이 보유한 회선 수", "col_001"),
    ("term_col_002", "가입자당평균매출", "ARPU,평균매출,average revenue per user", "과금", "가입자당 평균 매출", "col_002"),
]


def _dummy_vec(seed_text: str) -> list:
    random.seed(hash(seed_text) % (2**32))
    return [random.random() for _ in range(EMBED_DIM)]


def build_meta_db():
    os.makedirs(os.path.dirname(META_DB_PATH), exist_ok=True)
    con = duckdb.connect(META_DB_PATH)
    con.execute("INSTALL vss; LOAD vss;")

    con.execute("""
        CREATE OR REPLACE TABLE column_spec (
            column_id VARCHAR PRIMARY KEY,
            table_id VARCHAR,
            column_name VARCHAR,
            data_type VARCHAR,
            description VARCHAR,
            tag VARCHAR,
            confidence DOUBLE,
            updated_at TIMESTAMP
        )
    """)
    con.execute("""
        CREATE OR REPLACE TABLE table_relationships (
            from_table_id VARCHAR,
            to_table_id VARCHAR,
            join_key VARCHAR,
            relation_type VARCHAR
        )
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE column_embeddings (
            column_id VARCHAR,
            embedding FLOAT[{EMBED_DIM}]
        )
    """)
    con.execute("""
        CREATE OR REPLACE TABLE glossary_terms (
            term_id VARCHAR PRIMARY KEY,
            canonical_term VARCHAR,
            synonyms VARCHAR,
            domain VARCHAR,
            description VARCHAR,
            linked_column_id VARCHAR,
            source VARCHAR,
            updated_at TIMESTAMP
        )
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE glossary_embeddings (
            term_id VARCHAR,
            embedding FLOAT[{EMBED_DIM}]
        )
    """)

    for col_id, table_id, col_name, dtype, desc in SAMPLE_COLUMNS:
        con.execute(
            "INSERT INTO column_spec (column_id, table_id, column_name, data_type, description) "
            "VALUES (?, ?, ?, ?, ?)",
            [col_id, table_id, col_name, dtype, desc],
        )
        con.execute(
            "INSERT INTO column_embeddings (column_id, embedding) VALUES (?, ?)",
            [col_id, _dummy_vec(col_id)],
        )

    for from_id, to_id, join_key, rel_type in SAMPLE_RELATIONSHIPS:
        con.execute("INSERT INTO table_relationships VALUES (?, ?, ?, ?)", [from_id, to_id, join_key, rel_type])

    for term_id, canonical, synonyms, domain, desc, linked_col in SAMPLE_GLOSSARY:
        con.execute(
            "INSERT INTO glossary_terms "
            "(term_id, canonical_term, synonyms, domain, description, linked_column_id, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'manual', current_timestamp)",
            [term_id, canonical, synonyms, domain, desc, linked_col],
        )
        con.execute(
            "INSERT INTO glossary_embeddings (term_id, embedding) VALUES (?, ?)",
            [term_id, _dummy_vec(term_id)],
        )

    con.close()
    print(f"[meta db] {META_DB_PATH} 생성 완료 "
          f"({len(SAMPLE_COLUMNS)}개 컬럼, {len(SAMPLE_GLOSSARY)}개 용어)")


def build_data_db():
    os.makedirs(os.path.dirname(DATA_DB_PATH), exist_ok=True)
    con = duckdb.connect(DATA_DB_PATH)

    # col_004(LEGACY_FLAG)는 일부러 빼서 not_found 케이스를 만든다.
    # col_005(REGION_CD)는 명세(INTEGER)와 다르게 VARCHAR로 만들어 type_mismatch 케이스를 만든다.
    con.execute("""
        CREATE OR REPLACE TABLE raw_customer (
            customer_id VARCHAR,
            SUBS_LINE_CNT VARCHAR,
            ARPU INTEGER,
            REGION_CD VARCHAR,
            month INTEGER
        )
    """)
    con.execute("""
        CREATE OR REPLACE TABLE raw_call (
            customer_id VARCHAR,
            CALL_CNT INTEGER,
            month INTEGER
        )
    """)

    for m in [202401, 202402, 202403, 202404, 202405]:
        con.execute("INSERT INTO raw_customer VALUES ('C001', '2', 45000, 'SEOUL', ?)", [m])
        con.execute("INSERT INTO raw_call VALUES ('C001', 120, ?)", [m])

    con.close()
    print(f"[data db] {DATA_DB_PATH} 생성 완료 (raw_customer, raw_call / 보유기간 202401~202405)")


def build_audit_db():
    os.makedirs(os.path.dirname(AUDIT_DB_PATH), exist_ok=True)
    con = sqlite3.connect(AUDIT_DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS qna_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id VARCHAR,
            interaction_type VARCHAR,
            question TEXT,
            answer TEXT,
            round_no INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS revision_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id VARCHAR,
            before_tag VARCHAR,
            after_tag VARCHAR,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS validation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id VARCHAR,
            step VARCHAR,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS auto_confirm_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id VARCHAR,
            eng_name VARCHAR,
            confidence DOUBLE,
            evidence TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    con.commit()
    con.close()
    print(f"[audit db] {AUDIT_DB_PATH} 생성 완료 (auto_confirm_log 포함)")


if __name__ == "__main__":
    build_meta_db()
    build_data_db()
    build_audit_db()
    print("\n합성 fixture 생성 완료. 다음으로 실행해보세요:")
    print("  python -u run_pipeline.py data/sample_spec.xlsx")
    print("(주의: column_embeddings/glossary_embeddings는 더미 랜덤 벡터입니다.")
    print(" 실제 유사도 검색 동작을 검증하려면 실제 임베딩 API로 다시 생성하세요.)")
