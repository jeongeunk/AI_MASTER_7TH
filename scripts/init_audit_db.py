"""
감사 DB(schemascout_audit.sqlite) 초기화

validation_log   : DB Validation Agent 실행 이력
qna_history       : Agent-담당자 확인/문답 이력 (interaction_type으로 구분)
revision_history  : 명세 변경(태그 확정) 이력

실행: python init_audit_db.py
"""

import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()

AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")
os.makedirs(os.path.dirname(AUDIT_DB_PATH), exist_ok=True)


def main():
    con = sqlite3.connect(AUDIT_DB_PATH)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS validation_log (
            log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id       TEXT,
            query_executed  TEXT,
            result_summary  TEXT,
            executed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS qna_history (
            qna_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id        TEXT,
            interaction_type TEXT CHECK (interaction_type IN ('qna', 'confirmation')),
            question         TEXT,
            answer           TEXT,
            round_no         INTEGER,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS revision_history (
            revision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id       TEXT,
            before_tag      TEXT,
            after_tag       TEXT,
            reason          TEXT,
            revised_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.commit()

    tables = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print(f"[DONE] {AUDIT_DB_PATH} 생성 완료")
    print("생성된 테이블:", [t[0] for t in tables])

    con.close()


if __name__ == "__main__":
    main()
