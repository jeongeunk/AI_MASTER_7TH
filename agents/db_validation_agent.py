"""
DB Validation Agent

역할: matched + auto_confirmed + inferred_confirmed 컬럼에 한해 실데이터 DB에 접속해
      존재 여부·실제 type·보유 기간 조회. type 불일치 시 담당자 확인 후 최종 type 확정.

Guardrail:
- 메타 DB 세션에서 ATTACH ... READ_ONLY로 실데이터 DB 연결 (쓰기 원천 차단)
- 쿼리 구조 검증(단일 SELECT 문 여부, DDL/DML 포함 여부) - sqlglot 사용
- 결과 행 수 상한 적용

주의: 이 파일도 담당자 확인을 input()으로 대체했습니다.
      LangGraph 연결 시 request_type_confirmation 부분을 interrupt()로 교체합니다.
"""

import os
import sys
import sqlite3
import duckdb
import sqlglot
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.trace import tool_span

load_dotenv()

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")
DATA_DB_PATH = os.environ.get("DATA_DB_PATH", "./db/schemascout_data.duckdb")
AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")

MAX_ROWS = 100_000
QUERY_TIMEOUT_SEC = 30


# ── Step 0: Guardrail ───────────────────────────────────────
def get_guarded_connection():
    """
    메타 DB 세션에서 실데이터 DB를 READ_ONLY로 ATTACH.
    LangGraph interrupt() 재개 시 노드 함수가 처음부터 재실행되므로,
    이미 ATTACH되어 있으면 다시 시도하지 않도록 방어.
    """
    con = duckdb.connect(META_DB_PATH)
    attached = [row[0] for row in con.execute(
        "SELECT database_name FROM duckdb_databases()"
    ).fetchall()]
    if "data_db" not in attached:
        con.execute(f"ATTACH '{DATA_DB_PATH}' AS data_db (READ_ONLY)")
    return con


def validate_query_structure(raw_query: str) -> dict:
    """쿼리가 단일 SELECT 문인지, DDL/DML을 포함하는지 AST 기반 검증"""
    try:
        parsed = sqlglot.parse(raw_query, read="duckdb")
    except Exception as e:
        return {"is_valid": False, "violation_type": f"parse_error: {e}"}

    if len(parsed) != 1:
        return {"is_valid": False, "violation_type": "multiple_statements"}

    stmt = parsed[0]
    stmt_type = type(stmt).__name__
    if stmt_type != "Select":
        return {"is_valid": False, "violation_type": f"not_select ({stmt_type})"}

    forbidden = ("Insert", "Update", "Delete", "Drop", "Create", "Alter", "Merge")
    for node in stmt.walk():
        if type(node[0]).__name__ in forbidden:
            return {"is_valid": False, "violation_type": f"forbidden_clause ({type(node[0]).__name__})"}

    return {"is_valid": True, "violation_type": None}


# ── Tool: check_column_exists ──────────────────────────────
def check_column_exists(con, table: str, column: str) -> dict:
    with tool_span("check_column_exists"):
        row = con.execute(
            "SELECT COUNT(*) FROM duckdb_columns() "
            "WHERE database_name = 'data_db' AND table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()
    return {"exists": row[0] > 0}


# ── Tool: fetch_spec_type ──────────────────────────────────
def fetch_spec_type(con, column_id: str) -> str:
    with tool_span("fetch_spec_type"):
        row = con.execute(
            "SELECT data_type FROM column_spec WHERE column_id = ?", [column_id]
        ).fetchone()
    return row[0] if row else None


# ── Tool: query_column_type ────────────────────────────────
def query_column_type(con, table: str, column: str) -> str:
    with tool_span("query_column_type"):
        row = con.execute(
            "SELECT data_type FROM duckdb_columns() "
            "WHERE database_name = 'data_db' AND table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()
    return row[0] if row else None


# ── Tool: compare_type_immediate ───────────────────────────
def compare_type_immediate(spec_type: str, actual_type: str) -> bool:
    return str(spec_type).upper() == str(actual_type).upper()


# ── Tool: request_type_confirmation (담당자 확인) ───────────
def _console_confirm(payload: dict) -> str:
    """기본 확인 방식: 콘솔 input() (단독 실행/테스트용)"""
    print("\n" + "=" * 60)
    print("[담당자 확인 요청 - type 불일치]")
    print(f"  컬럼      : {payload['column_id']} (테이블: {payload['table']})")
    print(f"  명세 type : {payload['spec_type']}")
    print(f"  실제 type : {payload['actual_type']}")
    print("=" * 60)
    ans = input("실제 DB 기준(actual_type)으로 갱신하시겠습니까? (y/n): ").strip().lower()
    return "approved" if ans == "y" else "rejected"


def request_type_confirmation(spec_type: str, actual_type: str, column_id: str, table: str,
                               confirm_fn=None) -> str:
    """
    confirm_fn: payload(dict) -> "approved" | "rejected" 를 반환하는 콜백.
    None이면 콘솔 input()으로 동작(단독 실행용). LangGraph 노드에서는
    interrupt()를 호출하는 confirm_fn을 주입해 실제 HITL로 동작시킴.
    """
    payload = {
        "type": "type_mismatch_confirmation",
        "column_id": column_id,
        "table": table,
        "spec_type": spec_type,
        "actual_type": actual_type,
    }
    fn = confirm_fn or _console_confirm
    return fn(payload)


# ── Tool: apply_type_confirmation ──────────────────────────
def apply_type_confirmation(decision: str, spec_type: str, actual_type: str) -> dict:
    if decision == "approved":
        return {"final_type": actual_type, "type_match_status": "mismatch_confirmed"}
    return {"final_type": spec_type, "type_match_status": "mismatch_rejected"}


# ── Tool: log_confirmation_to_audit ────────────────────────
def log_confirmation_to_audit(column_id: str, question: str, answer: str, round_no: int = 1):
    """
    LangGraph interrupt() 재개 시 노드가 재실행되며 이미 처리된 확인도 다시 지나가므로,
    동일한 (column_id, question, answer) 조합이 이미 기록되어 있으면 중복 삽입하지 않음.
    """
    con = sqlite3.connect(AUDIT_DB_PATH)
    exists = con.execute(
        "SELECT 1 FROM qna_history WHERE column_id = ? AND question = ? AND answer = ? LIMIT 1",
        [column_id, question, answer],
    ).fetchone()
    if not exists:
        con.execute(
            "INSERT INTO qna_history (column_id, interaction_type, question, answer, round_no) "
            "VALUES (?, 'confirmation', ?, ?, ?)",
            [column_id, question, answer, round_no],
        )
        con.commit()
    con.close()


# ── Tool: detect_month_like_columns (보유기간 추정용) ────────
def detect_month_like_columns(con, table: str) -> list:
    """
    'month' 컬럼이 없는 테이블(주로 dim 테이블)에서 시점을 나타낼 것으로
    추정되는 컬럼을 찾는다. 컬럼명에 'month'가 포함되고, 값이 전부
    YYYYMM 형태(190001~299912, 뒤 2자리 01~12)인 컬럼만 후보로 인정한다.
    """
    candidates = con.execute(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE database_name = 'data_db' AND table_name = ? "
        "AND data_type IN ('BIGINT', 'INTEGER', 'HUGEINT', 'DOUBLE', 'FLOAT') "
        "AND lower(column_name) LIKE '%month%'",
        [table],
    ).fetchall()

    month_like = []
    for (column_name,) in candidates:
        total, valid = con.execute(
            f"SELECT COUNT(*), COUNT(*) FILTER ("
            f"CAST({column_name} AS BIGINT) BETWEEN 190001 AND 299912 "
            f"AND CAST({column_name} AS BIGINT) % 100 BETWEEN 1 AND 12"
            f") FROM data_db.{table} WHERE {column_name} IS NOT NULL"
        ).fetchone()
        if total and total == valid:
            month_like.append(column_name)
    return month_like


# ── Tool: query_retention_period ───────────────────────────
def query_retention_period(con, table: str, column: str) -> dict:
    with tool_span("query_retention_period"):
        has_month = con.execute(
            "SELECT COUNT(*) FROM duckdb_columns() "
            "WHERE database_name = 'data_db' AND table_name = ? AND column_name = 'month'",
            [table],
        ).fetchone()[0]
        if has_month:
            row = con.execute(f"SELECT MIN(month), MAX(month) FROM data_db.{table}").fetchone()
            return {"start": row[0], "end": row[1], "estimated": False, "estimated_from": None}

        # month 컬럼이 없는 dim 테이블 등: month로 추정되는 컬럼으로 보유기간 추정
        month_like_cols = detect_month_like_columns(con, table)
        if not month_like_cols:
            return {"start": None, "end": None, "estimated": False, "estimated_from": None}

        union_sql = " UNION ALL ".join(
            f"SELECT {c} AS m FROM data_db.{table} WHERE {c} IS NOT NULL"
            for c in month_like_cols
        )
        row = con.execute(f"SELECT MIN(m), MAX(m) FROM ({union_sql})").fetchone()
        start = int(row[0]) if row[0] is not None else None
        end = int(row[1]) if row[1] is not None else None
    return {"start": start, "end": end, "estimated": True, "estimated_from": month_like_cols}


# ── Tool: execute_readonly_query (Guardrail 적용된 실행기) ──
def execute_readonly_query(con, validated_query: str, timeout_sec: int = QUERY_TIMEOUT_SEC):
    check = validate_query_structure(validated_query)
    if not check["is_valid"]:
        raise PermissionError(f"Guardrail 위반: {check['violation_type']}")

    limited_query = f"SELECT * FROM ({validated_query}) LIMIT {MAX_ROWS}"
    return con.execute(limited_query).fetchdf()


# ── 오케스트레이션 ───────────────────────────────────────────
def run_db_validation(meta_search_results: list, confirm_fn=None) -> list:
    """
    meta_search_results: meta_search_agent.run_meta_search(...) 결과
    matched / auto_confirmed / inferred_confirmed 인 행만 실제 검증 수행, 그 외는 그대로 통과(존재여부 없이 skip)
    """
    con = get_guarded_connection()
    results = []

    for row in meta_search_results:
        if row["match_status"] not in ("matched", "auto_confirmed", "inferred_confirmed"):
            # unresolved는 Meta Search Agent에서 이미 종결되어 여기 안 옴 (안전장치)
            continue

        meta_row = row["meta_row"]
        table_id = meta_row["table_id"]
        column_name = meta_row["column_name"]
        column_id = meta_row["column_id"]

        exists_check = check_column_exists(con, table_id, column_name)
        out = dict(row)
        out["source_table"] = table_id
        out["exists"] = exists_check["exists"]

        spec_type = fetch_spec_type(con, column_id)
        out["spec_type"] = spec_type  # 요청 type (명세서/메타 DB 기준)

        if not exists_check["exists"]:
            out["actual_type"] = None
            out["final_type"] = None
            out["type_match_status"] = None
            out["retention_period"] = None
            results.append(out)
            continue

        actual_type = query_column_type(con, table_id, column_name)
        out["actual_type"] = actual_type  # 제공가능 type (실제 DB 기준)
        match = compare_type_immediate(spec_type, actual_type)

        if match:
            out["final_type"] = actual_type
            out["type_match_status"] = "matched"
        else:
            decision = request_type_confirmation(spec_type, actual_type, column_id, table_id, confirm_fn)
            log_confirmation_to_audit(
                column_id,
                question=f"{column_id}: spec={spec_type} vs actual={actual_type}, DB 기준 갱신?",
                answer=decision,
            )
            confirm = apply_type_confirmation(decision, spec_type, actual_type)
            out["final_type"] = confirm["final_type"]
            out["type_match_status"] = confirm["type_match_status"]

        out["retention_period"] = query_retention_period(con, table_id, column_name)
        results.append(out)

    con.close()
    return results


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agents.parsing_agent import run_parsing
    from agents.meta_search_agent import run_meta_search
    from llm_client import embed

    file_path = sys.argv[1] if len(sys.argv) > 1 else "./data/sample_spec.xlsx"
    parsed = run_parsing(file_path)
    meta_results = run_meta_search(parsed["parsed_rows"], embed)
    validation_results = run_db_validation(meta_results)

    print("\n" + "=" * 60)
    print("[DB Validation 결과 요약]")
    print("=" * 60)
    for r in validation_results:
        print(f"  {r['영문명']:25s} exists={r['exists']!s:6s} "
              f"final_type={r.get('final_type')!s:10s} status={r.get('type_match_status')}")
