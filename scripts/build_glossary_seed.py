"""
[로컬 실행 전용] glossary_terms 시드 구축 (1/3단계)

column_spec의 한글명/영문명/description을 규칙 기반으로 정규화해
표기 변형 후보(초안 동의어 그룹)를 만든다. 이 결과는 LLM으로 보강되기 전
초안이며, 사람이 검수하기 전까지 source='draft'로 표시한다.

실행 전 확인:
- schemascout_meta.duckdb가 로컬 db/ 폴더에 있어야 함
- column_spec.description이 "한글명 | 설명" 형태로 채워져 있어야 함(fill_meta_db.py 결과)

실행: python scripts/build_glossary_seed.py
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")


def _normalize(text: str) -> str:
    """공백/언더스코어/특수문자 제거 + 소문자화 — 표기 변형 그룹핑용"""
    return re.sub(r"[^a-z0-9가-힣]", "", str(text).lower())


def build_seed():
    con = duckdb.connect(META_DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS glossary_terms (
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

    rows = con.execute("SELECT column_id, column_name, description FROM column_spec").fetchall()

    seed_count = 0
    for column_id, column_name, description in rows:
        kor_name = description.split(" | ", 1)[0] if description and " | " in description else column_name
        term_id = f"term_{column_id}"

        exists = con.execute("SELECT 1 FROM glossary_terms WHERE term_id = ?", [term_id]).fetchone()
        if exists:
            continue

        # 표기 변형 후보: 원본 영문명, 정규화된 영문명, 한글명 자체를 동의어 시드로 등록
        synonyms = ",".join(sorted(set([column_name, kor_name])))

        con.execute(
            "INSERT INTO glossary_terms "
            "(term_id, canonical_term, synonyms, domain, description, linked_column_id, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'draft', current_timestamp)",
            [term_id, kor_name, synonyms, None, description, column_id],
        )
        seed_count += 1

    con.close()
    print(f"[build_glossary_seed] {seed_count}건 초안 등록 완료 (source='draft')")
    print("다음 단계: python scripts/llm_expand_glossary.py")


if __name__ == "__main__":
    build_seed()
