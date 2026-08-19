"""
로컬 환경 검증 스크립트

1. DuckDB 설치 확인
2. vss 확장 설치 (최초 1회만 인터넷 필요, 이후 로컬 캐시 사용)
3. HNSW 인덱스 생성까지 실제로 테스트
4. 메타 DB(schemascout_meta.duckdb) 뼈대 테이블 생성

실행: python test_setup.py
"""

import os
import duckdb

DB_DIR = "./db"
META_DB_PATH = os.path.join(DB_DIR, "schemascout_meta.duckdb")

os.makedirs(DB_DIR, exist_ok=True)


def check_duckdb_version():
    print(f"[1/4] DuckDB 버전: {duckdb.__version__}")


def install_vss(con: duckdb.DuckDBPyConnection):
    print("[2/4] vss 확장 설치 중 (최초 1회 인터넷 필요)...")
    con.execute("INSTALL vss;")
    con.execute("LOAD vss;")
    con.execute("SET hnsw_enable_experimental_persistence = true;")

    result = con.execute(
        "SELECT extension_name, loaded, installed "
        "FROM duckdb_extensions() WHERE extension_name = 'vss'"
    ).fetchall()
    print(f"       설치 상태: {result}")
    if not result or not result[0][1]:
        raise RuntimeError("vss 확장이 로드되지 않았습니다. 네트워크(프록시) 설정을 확인하세요.")


def test_hnsw_index(con: duckdb.DuckDBPyConnection):
    print("[3/4] HNSW 인덱스 생성 테스트 중...")
    con.execute("DROP TABLE IF EXISTS _vss_smoke_test")
    con.execute("CREATE TABLE _vss_smoke_test (id INT, vec FLOAT[4])")
    con.execute("INSERT INTO _vss_smoke_test VALUES (1, [0.1,0.2,0.3,0.4]), (2, [0.4,0.3,0.2,0.1])")
    con.execute(
        "CREATE INDEX idx_vss_smoke ON _vss_smoke_test USING HNSW (vec) WITH (metric = 'cosine')"
    )
    r = con.execute(
        "SELECT id FROM _vss_smoke_test ORDER BY array_cosine_distance(vec, [0.1,0.2,0.3,0.4]::FLOAT[4]) LIMIT 1"
    ).fetchone()
    print(f"       유사도 검색 테스트 결과 (id=1이 나와야 정상): {r}")
    con.execute("DROP TABLE _vss_smoke_test")


def init_meta_schema(con: duckdb.DuckDBPyConnection):
    print("[4/4] 메타 DB 뼈대 테이블 생성 중...")
    con.execute("""
        CREATE TABLE IF NOT EXISTS table_catalog (
            table_id      VARCHAR PRIMARY KEY,
            table_name    VARCHAR NOT NULL,
            source_system VARCHAR,
            registered_at TIMESTAMP DEFAULT current_timestamp,
            row_count     BIGINT,
            description   TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS column_spec (
            column_id   VARCHAR PRIMARY KEY,
            table_id    VARCHAR NOT NULL,
            column_name VARCHAR NOT NULL,
            data_type   VARCHAR,
            description TEXT,
            tag         VARCHAR,
            confidence  DOUBLE,
            updated_at  TIMESTAMP DEFAULT current_timestamp
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS column_embeddings (
            column_id VARCHAR PRIMARY KEY,
            embedding FLOAT[3072]
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS table_relationships (
            relationship_id VARCHAR PRIMARY KEY,
            from_table_id   VARCHAR NOT NULL,
            to_table_id     VARCHAR NOT NULL,
            join_key        VARCHAR NOT NULL,
            relation_type   VARCHAR,
            source          VARCHAR DEFAULT 'manual',
            confidence      DOUBLE,
            created_at      TIMESTAMP DEFAULT current_timestamp
        )
    """)
    tables = con.execute("SHOW TABLES").fetchall()
    print(f"       생성된 테이블: {[t[0] for t in tables]}")


if __name__ == "__main__":
    check_duckdb_version()
    con = duckdb.connect(META_DB_PATH)
    install_vss(con)
    test_hnsw_index(con)
    init_meta_schema(con)
    con.close()
    print(f"\n모든 검증 통과. 메타 DB 파일: {META_DB_PATH}")
