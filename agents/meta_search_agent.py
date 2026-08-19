"""
Meta Search Agent

역할: 컬럼(영문명/한글명/항목설명)을 메타 DB와 대조하여 matched/inferred/unresolved 판정.
- 정확 매칭 실패 시 유사도 검색(vss) -> 임계값 이상이면 담당자 확인(interrupt) 후 확정
- 거절 시 unresolved로 즉시 확정하고 메타 DB에 직접 기록 (DB Validation/Classification 미경유)

주의: 이 파일은 LangGraph interrupt() 없이 단독 실행 가능하도록
      담당자 확인을 input()으로 대체한 버전입니다.
      LangGraph 연결(추후 단계)에서는 request_inferred_confirmation 부분을
      interrupt()로 교체하게 됩니다.
"""

import os
import sqlite3
import duckdb
from dotenv import load_dotenv

load_dotenv()

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")
AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")
SIMILARITY_THRESHOLD = 0.75  # cosine distance 기준 (낮을수록 유사) -> 유사도 = 1 - distance


# ── Tool 1: exact_match_meta_db ────────────────────────────
def exact_match_meta_db(con, eng_name: str) -> dict:
    row = con.execute(
        "SELECT column_id, table_id, column_name, data_type, description "
        "FROM column_spec WHERE column_name = ?",
        [eng_name],
    ).fetchone()
    if row:
        return {"found": True, "meta_row": {
            "column_id": row[0], "table_id": row[1],
            "column_name": row[2], "data_type": row[3], "description": row[4],
        }}
    return {"found": False, "meta_row": None}


# ── Tool 2: semantic_search_meta (vss) ─────────────────────
def semantic_search_meta(con, description: str, embed_fn, top_k: int = 3) -> list:
    """
    embed_fn: llm_client.embed 를 감싼 함수. text -> vector(list[float])
    vss 확장이 로드되어 있어야 함 (con.execute("LOAD vss;") 사전 실행 필요)
    """
    resp = embed_fn("DEPLOYMENT_EMBED_LARGE", description)
    query_vec = resp.data[0].embedding

    rows = con.execute(
        """
        SELECT cs.column_id, cs.table_id, cs.column_name, cs.data_type, cs.description,
               array_cosine_distance(ce.embedding, ?::FLOAT[3072]) AS distance
        FROM column_embeddings ce
        JOIN column_spec cs ON ce.column_id = cs.column_id
        ORDER BY distance ASC
        LIMIT ?
        """,
        [query_vec, top_k],
    ).fetchall()

    results = []
    for r in rows:
        similarity = 1 - r[5]
        results.append({
            "meta_row": {
                "column_id": r[0], "table_id": r[1], "column_name": r[2],
                "data_type": r[3], "description": r[4],
            },
            "similarity_score": round(similarity, 4),
        })
    return results


# ── Tool 3: tag_match_result ───────────────────────────────
def tag_match_result(similarity_score: float, threshold: float = SIMILARITY_THRESHOLD) -> dict:
    if similarity_score >= threshold:
        return {"tag": "inferred_pending", "evidence": f"유사도 {similarity_score} >= 임계값 {threshold}"}
    return {"tag": "unresolved", "evidence": f"유사도 {similarity_score} < 임계값 {threshold}"}


# ── Tool 4: request_inferred_confirmation (담당자 확인) ────
def _console_confirm(payload: dict) -> str:
    """기본 확인 방식: 콘솔 input() (단독 실행/테스트용)"""
    print("\n" + "=" * 60)
    print("[담당자 확인 요청 - inferred 후보]")
    print(f"  원본 컬럼   : {payload['eng_name']} / {payload['kor_name']}")
    print(f"  원본 설명   : {payload['description']}")
    print(f"  매칭 후보   : {payload['candidate_column']} ({payload['candidate_table']})")
    print(f"  후보 설명   : {payload['candidate_description']}")
    print(f"  유사도 점수 : {payload['similarity_score']}")
    print("=" * 60)
    ans = input("이 매칭을 승인하시겠습니까? (y/n): ").strip().lower()
    return "approved" if ans == "y" else "rejected"


def request_inferred_confirmation(column_meta: dict, matched_meta_row: dict, similarity_score: float,
                                   confirm_fn=None) -> str:
    """
    confirm_fn: payload(dict) -> "approved" | "rejected" 를 반환하는 콜백.
    None이면 콘솔 input()으로 동작(단독 실행용). LangGraph 노드에서는
    interrupt()를 호출하는 confirm_fn을 주입해 실제 HITL로 동작시킴.
    """
    payload = {
        "type": "inferred_confirmation",
        "eng_name": column_meta.get("영문명"),
        "kor_name": column_meta.get("한글명"),
        "description": column_meta.get("항목설명"),
        "candidate_column": matched_meta_row["column_name"],
        "candidate_table": matched_meta_row["table_id"],
        "candidate_description": matched_meta_row["description"],
        "similarity_score": similarity_score,
    }
    fn = confirm_fn or _console_confirm
    return fn(payload)


# ── Tool 5: apply_confirmation_result ──────────────────────
def apply_confirmation_result(decision: str) -> dict:
    if decision == "approved":
        return {"final_tag": "inferred_confirmed", "confirmation_status": "approved"}
    return {"final_tag": "unresolved", "confirmation_status": "rejected"}


# ── Tool 6: update_meta_tag (Classification Agent와 공유) ──
def update_meta_tag(con, column_id: str, tag: str, confidence: float = None):
    con.execute(
        "UPDATE column_spec SET tag = ?, confidence = ?, updated_at = current_timestamp WHERE column_id = ?",
        [tag, confidence, column_id],
    )


# ── Tool 7: log_confirmation_to_audit ──────────────────────
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


# ── Tool 8: get_table_relationships ────────────────────────
def get_table_relationships(con, table_id: str) -> list:
    rows = con.execute(
        "SELECT from_table_id, to_table_id, join_key, relation_type "
        "FROM table_relationships WHERE from_table_id = ? OR to_table_id = ?",
        [table_id, table_id],
    ).fetchall()
    return [
        {"source_table": r[0], "target_table": r[1], "join_key": r[2], "relation_type": r[3]}
        for r in rows
    ]


# ── 오케스트레이션 ───────────────────────────────────────────
def run_meta_search(parsed_rows: list, embed_fn, confirm_fn=None) -> list:
    """
    parsed_rows: parsing_agent.run_parsing(...)의 state['parsed_rows']
    반환: 각 행에 match_status, table_relationships 등이 추가된 결과 리스트
    (matched / inferred_confirmed 만 다음 단계로 넘어갈 대상, unresolved는 여기서 종결)
    """
    con = duckdb.connect(META_DB_PATH)
    con.execute("LOAD vss;")

    results = []
    for row in parsed_rows:
        eng_name = str(row["영문명"]).strip()
        unresolved_reason = None

        # Step 1: 정확 매칭
        exact = exact_match_meta_db(con, eng_name)

        if exact["found"]:
            match_status = "matched"
            meta_row = exact["meta_row"]
            evidence = "영문명 정확 매칭"
        else:
            # Step 2: 유사도 검색
            candidates = semantic_search_meta(con, str(row["항목설명"]), embed_fn, top_k=1)
            if not candidates:
                match_status = "unresolved"
                meta_row = None
                unresolved_reason = "no_match"
                evidence = "유사도 검색 결과 없음"
            else:
                top = candidates[0]
                tag_result = tag_match_result(top["similarity_score"])

                if tag_result["tag"] == "unresolved":
                    match_status = "unresolved"
                    meta_row = None
                    unresolved_reason = "no_match"
                    evidence = tag_result["evidence"]
                else:
                    # Step 3: 담당자 확인 (interrupt 대체)
                    decision = request_inferred_confirmation(row, top["meta_row"], top["similarity_score"], confirm_fn)
                    confirm_result = apply_confirmation_result(decision)
                    log_confirmation_to_audit(
                        top["meta_row"]["column_id"],
                        question=f"{eng_name} -> {top['meta_row']['column_name']} 매칭(유사도 {top['similarity_score']}) 승인?",
                        answer=decision,
                    )
                    match_status = confirm_result["final_tag"]  # inferred_confirmed 또는 unresolved
                    meta_row = top["meta_row"]  # 거절 시에도 후보 정보는 유지(태깅에 필요) - DB Validation은 matched/inferred_confirmed만 진행하므로 안전
                    unresolved_reason = "rejected_by_human" if match_status == "unresolved" else None
                    evidence = f"유사도 매칭 후 담당자 {decision}"

        # unresolved는 여기서 메타 DB에 직접 태그 확정 (Classification 미경유)
        if match_status == "unresolved" and meta_row is not None:
            update_meta_tag(con, meta_row["column_id"], "unresolved")
        # meta_row가 None인 unresolved(후보 자체가 없던 경우)는 태깅할 대상이 없어 감사 로그만 남김(별도 처리 없음)

        result_row = dict(row)
        result_row["match_status"] = match_status
        result_row["match_evidence"] = evidence
        result_row["meta_row"] = meta_row
        result_row["unresolved_reason"] = unresolved_reason if match_status == "unresolved" else None

        if match_status in ("matched", "inferred_confirmed") and meta_row:
            result_row["table_relationships"] = get_table_relationships(con, meta_row["table_id"])
        else:
            result_row["table_relationships"] = []

        results.append(result_row)

    con.close()
    return results


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agents.parsing_agent import run_parsing
    from llm_client import embed

    file_path = sys.argv[1] if len(sys.argv) > 1 else "./data/sample_spec.xlsx"
    parsed = run_parsing(file_path)

    results = run_meta_search(parsed["parsed_rows"], embed)

    print("\n" + "=" * 60)
    print("[Meta Search 결과 요약]")
    print("=" * 60)
    counts = {}
    for r in results:
        counts[r["match_status"]] = counts.get(r["match_status"], 0) + 1
    for status, cnt in counts.items():
        print(f"  {status}: {cnt}건")

    print("\n[상세]")
    for r in results:
        print(f"  {r['영문명']:30s} -> {r['match_status']:20s} ({r['match_evidence']})")