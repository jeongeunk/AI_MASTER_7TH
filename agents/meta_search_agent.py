"""
Meta Search Agent (RAG 전환판)

역할: 컬럼(영문명/한글명/항목설명)을 메타 DB와 대조하여 matched/auto_confirmed/
      inferred_confirmed/unresolved 판정.
- 정확 매칭 실패 시 다중소스 검색(컬럼 임베딩 + 용어집 임베딩 + 문자열 유사도)으로
  후보를 모으고, LLM(gpt-5-mini)이 후보 중 최적 매칭·확신도·근거를 생성한다.
- 확신도 >= AUTO_CONFIRM_CONFIDENCE  -> 담당자 확인 없이 자동 확정(auto_confirmed)
  확신도 in [RETRY_CONFIDENCE_FLOOR, AUTO_CONFIRM_CONFIDENCE) 이고 재검색 여지 있음
                                     -> 검색 조건을 넓혀 재검색(최대 MAX_RETRIEVAL_ATTEMPTS회)
  그 외                              -> 담당자 확인(interrupt)
- 거절 시 unresolved로 즉시 확정하고 메타 DB에 직접 기록 (DB Validation/Classification 미경유)

주의: 이 파일은 LangGraph interrupt() 없이 단독 실행 가능하도록
      담당자 확인을 input()으로 대체한 버전입니다(run_meta_search).
      LangGraph 연결(agents/langgraph_pipeline.py)에서는 동일한 아래 함수들을
      재사용하되, request_inferred_confirmation 부분을 interrupt() 기반
      confirm_fn으로 교체해 그래프 노드로 분해합니다.
"""

import os
import sys
import json
import sqlite3
from typing import Literal, Optional

import duckdb
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from rapidfuzz import process as fuzz_process, fuzz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.trace import tool_span, instrument_agent

load_dotenv()

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")
AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")

# ── 확신도/재검색 파라미터 (임계값 단일값 0.75/0.85 불일치를 대체) ──────────
SIMILARITY_PREFILTER_FLOOR = 0.75   # retrieve_candidates 1차 후보 필터 cosine 컷오프
AUTO_CONFIRM_CONFIDENCE = 0.92      # 이 이상이면 담당자 확인 없이 자동 확정
RETRY_CONFIDENCE_FLOOR = 0.70       # 이 미만이면 재검색 없이 바로 담당자 확인
MAX_RETRIEVAL_ATTEMPTS = 2          # 재검색 최대 횟수


# ── Tool: exact_match_meta_db ──────────────────────────────
def exact_match_meta_db(con, eng_name: str) -> dict:
    with tool_span("exact_match_meta_db"):
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


# ── Tool: fuzzy_match_candidates ───────────────────────────
def _fetch_all_columns(con) -> list:
    rows = con.execute(
        "SELECT column_id, table_id, column_name, data_type, description FROM column_spec"
    ).fetchall()
    return [
        {"column_id": r[0], "table_id": r[1], "column_name": r[2], "data_type": r[3], "description": r[4]}
        for r in rows
    ]


def fuzzy_match_candidates(eng_name: str, candidate_pool: list, top_k: int = 5) -> list:
    """rapidfuzz token_sort_ratio(0~100) 기준 상위 후보. 점수는 0~1로 정규화해 반환."""
    if not candidate_pool:
        return []
    choices = {c["column_id"]: c["column_name"] for c in candidate_pool}
    matches = fuzz_process.extract(
        eng_name, choices, scorer=fuzz.token_sort_ratio, limit=top_k
    )
    results = []
    for _match_name, score, column_id in matches:
        if score < 70:  # 70점 미만은 근거로 쓰기엔 너무 약함
            continue
        results.append({"column_id": column_id, "score": round(score / 100, 4)})
    return results


# ── Tool: retrieve_candidates (semantic_search_meta 대체) ──
def retrieve_candidates(con, eng_name: str, description: str, embed_fn,
                         top_k: int = 5, floor: float = SIMILARITY_PREFILTER_FLOOR,
                         include_glossary_boost: bool = False) -> list:
    """
    column_embeddings(vss) + glossary_embeddings(vss, linked_column_id 있는 것만) +
    문자열 유사도(fuzzy)를 병합해 후보 top_k를 반환한다.
    각 후보: {column_id, source: 'vss_column'|'vss_glossary'|'fuzzy', score, meta_row}
    """
    with tool_span("embed (retrieve_candidates)", model="text-embedding-3-large"):
        resp = embed_fn("DEPLOYMENT_EMBED_LARGE", description)
    query_vec = resp.data[0].embedding

    merged = {}  # column_id -> candidate dict (가장 점수 높은 것 유지)

    def _consider(column_id, score, source, meta_row):
        if score < floor:
            return
        existing = merged.get(column_id)
        if existing is None or score > existing["score"]:
            merged[column_id] = {"column_id": column_id, "source": source, "score": round(score, 4), "meta_row": meta_row}

    # 1) 컬럼 설명 임베딩 유사도
    with tool_span("vss_search (column_embeddings)"):
        col_rows = con.execute(
            """
            SELECT cs.column_id, cs.table_id, cs.column_name, cs.data_type, cs.description,
                   array_cosine_distance(ce.embedding, ?::FLOAT[3072]) AS distance
            FROM column_embeddings ce
            JOIN column_spec cs ON ce.column_id = cs.column_id
            ORDER BY distance ASC
            LIMIT ?
            """,
            [query_vec, max(top_k * 2, 5)],
        ).fetchall()
    for r in col_rows:
        similarity = 1 - r[5]
        meta_row = {"column_id": r[0], "table_id": r[1], "column_name": r[2], "data_type": r[3], "description": r[4]}
        _consider(r[0], similarity, "vss_column", meta_row)

    # 2) 도메인 용어집(glossary) 임베딩 유사도 — linked_column_id가 있는 것만 후보로 승격
    glossary_floor = (floor - 0.15) if include_glossary_boost else floor
    try:
        with tool_span("vss_search (glossary_embeddings)"):
            gloss_rows = con.execute(
                """
                SELECT gt.linked_column_id, gt.canonical_term,
                       array_cosine_distance(ge.embedding, ?::FLOAT[3072]) AS distance
                FROM glossary_embeddings ge
                JOIN glossary_terms gt ON ge.term_id = gt.term_id
                WHERE gt.linked_column_id IS NOT NULL
                ORDER BY distance ASC
                LIMIT ?
                """,
                [query_vec, max(top_k * 2, 5)],
            ).fetchall()
        for r in gloss_rows:
            similarity = 1 - r[2]
            if similarity < glossary_floor:
                continue
            meta_row_q = con.execute(
                "SELECT column_id, table_id, column_name, data_type, description FROM column_spec WHERE column_id = ?",
                [r[0]],
            ).fetchone()
            if meta_row_q:
                meta_row = {"column_id": meta_row_q[0], "table_id": meta_row_q[1], "column_name": meta_row_q[2],
                            "data_type": meta_row_q[3], "description": meta_row_q[4]}
                _consider(r[0], similarity, "vss_glossary", meta_row)
    except duckdb.Error:
        # glossary_embeddings가 아직 채워지지 않은 초기 환경에서도 파이프라인이 죽지 않도록 방어
        pass

    # 3) 문자열 유사도 (오탈자·표기 변형)
    with tool_span("fuzzy_match_candidates"):
        all_columns = _fetch_all_columns(con)
        fuzzy_results = fuzzy_match_candidates(eng_name, all_columns, top_k=top_k)
    for fm in fuzzy_results:
        cid = fm["column_id"]
        if cid in merged:
            continue  # vss가 이미 더 강한 근거로 잡았으면 fuzzy로 덮어쓰지 않음
        meta_row_q = next((c for c in all_columns if c["column_id"] == cid), None)
        if meta_row_q:
            _consider(cid, fm["score"], "fuzzy", meta_row_q)

    candidates = sorted(merged.values(), key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


# ── Tool: generate_match_judgment (LLM 호출) ────────────────
_JUDGE_SYSTEM_PROMPT = """너는 통신사 데이터 명세서의 컬럼을 메타 DB 후보와 매칭하는 감사관이다.
아래 "원본 컬럼 정보"와 "검색된 후보 목록"만 근거로 판단한다.
후보 목록에 없는 컬럼을 임의로 만들어내지 않는다(selected_column_id는 후보의 column_id 중 하나이거나,
적합한 후보가 없으면 null로 응답한다).
recommend_action 기준:
- confidence가 매우 높고 후보가 명확히 하나로 좁혀지면 auto_confirm
- 후보가 여러 개 비슷한 점수이거나 근거가 약하면 retry
- 그래도 애매하거나 confidence가 낮으면 human_confirm"""


class MatchJudgment(BaseModel):
    """generate_match_judgment의 Structured Output 스키마. API가 이 형태를 벗어난
    응답을 만들 수 없도록 강제하므로, 별도의 JSON 파싱/방어 코드가 필요 없다."""
    selected_column_id: Optional[str] = Field(
        default=None, description="후보 중 하나의 column_id. 적합한 후보가 없으면 null."
    )
    confidence: float = Field(description="0.0~1.0 사이의 확신도")
    evidence: str = Field(description="40자 내외, 왜 이 후보를 선택했는지(또는 왜 못 골랐는지)")
    recommend_action: Literal["auto_confirm", "retry", "human_confirm"]


def generate_match_judgment(column_meta: dict, candidates: list, chat_fn=None) -> dict:
    """
    chat_fn: llm_client.chat_parsed 를 감싼 함수. None이면 llm_client.chat_parsed를
             직접 import해서 사용. response_format=MatchJudgment로 Structured Output을
             요청하며, resp.choices[0].message.parsed 에 검증된 인스턴스가 채워진다.
    반환: {selected_column_id, confidence, evidence, recommend_action, hallucination_flag}
    """
    if chat_fn is None:
        from llm_client import chat_parsed as chat_fn  # 지연 import (테스트에서 mock 주입 용이하게)

    candidate_ids = {c["column_id"] for c in candidates}
    payload = {
        "원본_컬럼": {
            "영문명": column_meta.get("영문명"),
            "한글명": column_meta.get("한글명"),
            "항목설명": column_meta.get("항목설명"),
        },
        "후보_목록": [
            {
                "column_id": c["column_id"],
                "column_name": c["meta_row"]["column_name"],
                "table_id": c["meta_row"]["table_id"],
                "description": c["meta_row"]["description"],
                "source": c["source"],
                "score": c["score"],
            }
            for c in candidates
        ],
    }

    with tool_span("generate_match_judgment", model="gpt-5-mini"):
        resp = chat_fn(
            "DEPLOYMENT_GPT5_MINI",
            [
                {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format=MatchJudgment,
            # gpt-5-mini는 reasoning 계열이라 temperature 커스텀 값을 지원하지 않음
            # (400 Unsupported value: 'temperature' ... Only the default (1) value is supported.)
        )

    message = resp.choices[0].message
    judgment = getattr(message, "parsed", None)
    if judgment is None:
        reason = getattr(message, "refusal", None) or "Structured Output 응답 없음"
        return {"selected_column_id": None, "confidence": 0.0,
                "evidence": f"LLM 응답 파싱 실패: {reason}", "recommend_action": "human_confirm",
                "hallucination_flag": False}

    selected = judgment.selected_column_id
    confidence = max(0.0, min(1.0, judgment.confidence))
    evidence = judgment.evidence
    recommend_action = judgment.recommend_action
    hallucination_flag = False

    if selected is not None and selected not in candidate_ids:
        # 후보 목록 밖 응답 -> hallucination으로 간주, 안전한 경로로 강제 폴백
        hallucination_flag = True
        selected = None
        confidence = 0.0
        evidence = (evidence + " (검증 실패: 후보 목록 밖 응답)").strip()
        recommend_action = "human_confirm"

    return {
        "selected_column_id": selected,
        "confidence": confidence,
        "evidence": evidence,
        "recommend_action": recommend_action,
        "hallucination_flag": hallucination_flag,
    }


# ── Tool: decide_route (route_by_judgment의 순수함수 버전) ─
def decide_route(judgment: dict, retrieval_attempts: int) -> str:
    conf = judgment["confidence"]
    if conf >= AUTO_CONFIRM_CONFIDENCE:
        return "auto_confirm"
    if conf < RETRY_CONFIDENCE_FLOOR:
        return "human_confirm"
    if retrieval_attempts < MAX_RETRIEVAL_ATTEMPTS:
        return "retry"
    return "human_confirm"  # 재검색 소진 시 폴백


# ── Tool: expand_retrieval_params ──────────────────────────
def expand_retrieval_params(attempt_no: int) -> dict:
    return {
        "top_k": 5 + attempt_no * 3,
        "floor": max(0.55, SIMILARITY_PREFILTER_FLOOR - attempt_no * 0.10),
        "include_glossary_boost": True,
    }


# ── Tool: request_inferred_confirmation (담당자 확인) ──────
def _console_confirm(payload: dict) -> str:
    print("\n" + "=" * 60)
    print("[담당자 확인 요청 - inferred 후보]")
    print(f"  원본 컬럼   : {payload['eng_name']} / {payload['kor_name']}")
    print(f"  원본 설명   : {payload['description']}")
    print(f"  LLM 확신도  : {payload['llm_confidence']}")
    print(f"  LLM 근거    : {payload['llm_evidence']}")
    for c in payload["candidates"]:
        print(f"    - 후보: {c['column_name']} ({c['source']}, score={c['score']})")
    print("=" * 60)
    ans = input("이 매칭을 승인하시겠습니까? (y/n): ").strip().lower()
    return "approved" if ans == "y" else "rejected"


def request_inferred_confirmation(column_meta: dict, candidates: list, judgment: dict,
                                   confirm_fn=None) -> str:
    payload = {
        "type": "inferred_confirmation",
        "eng_name": column_meta.get("영문명"),
        "kor_name": column_meta.get("한글명"),
        "description": column_meta.get("항목설명"),
        "llm_confidence": judgment["confidence"],
        "llm_evidence": judgment["evidence"],
        "candidates": [
            {"column_id": c["column_id"], "column_name": c["meta_row"]["column_name"],
             "source": c["source"], "score": c["score"]}
            for c in candidates
        ],
    }
    fn = confirm_fn or _console_confirm
    return fn(payload)


# ── Tool: apply_confirmation_result ────────────────────────
def apply_confirmation_result(decision: str) -> dict:
    if decision == "approved":
        return {"final_tag": "inferred_confirmed", "confirmation_status": "approved"}
    return {"final_tag": "unresolved", "confirmation_status": "rejected"}


# ── Tool: update_meta_tag (Classification Agent와 공유) ────
def update_meta_tag(con, column_id: str, tag: str, confidence: float = None):
    with tool_span("update_meta_tag"):
        con.execute(
            "UPDATE column_spec SET tag = ?, confidence = ?, updated_at = current_timestamp WHERE column_id = ?",
            [tag, confidence, column_id],
        )


# ── Tool: log_auto_confirm (신규, 자동 확정 전량 감사 기록) ─
def _ensure_auto_confirm_log_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS auto_confirm_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id VARCHAR,
            eng_name VARCHAR,
            confidence DOUBLE,
            evidence TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def log_auto_confirm(column_id: str, eng_name: str, confidence: float, evidence: str):
    con = sqlite3.connect(AUDIT_DB_PATH)
    _ensure_auto_confirm_log_table(con)
    con.execute(
        "INSERT INTO auto_confirm_log (column_id, eng_name, confidence, evidence) VALUES (?, ?, ?, ?)",
        [column_id, eng_name, confidence, evidence],
    )
    con.commit()
    con.close()


# ── Tool: log_confirmation_to_audit ────────────────────────
def log_confirmation_to_audit(column_id: str, question: str, answer: str, round_no: int = 1):
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


# ── Tool: get_table_relationships ──────────────────────────
def get_table_relationships(con, table_id: str) -> list:
    with tool_span("get_table_relationships"):
        rows = con.execute(
            "SELECT from_table_id, to_table_id, join_key, relation_type "
            "FROM table_relationships WHERE from_table_id = ? OR to_table_id = ?",
            [table_id, table_id],
        ).fetchall()
    return [
        {"source_table": r[0], "target_table": r[1], "join_key": r[2], "relation_type": r[3]}
        for r in rows
    ]


def _enrich_relationships(con, result: dict) -> dict:
    if result.get("meta_row"):
        result["table_relationships"] = get_table_relationships(con, result["meta_row"]["table_id"])
    else:
        result["table_relationships"] = []
    return result


# ── 오케스트레이션 (단독 실행용 — LangGraph 없이 순수 파이썬 루프) ──
def run_meta_search(parsed_rows: list, embed_fn, confirm_fn=None, chat_fn=None) -> list:
    """
    parsed_rows: parsing_agent.run_parsing(...)의 state['parsed_rows']
    반환: 각 행에 match_status, resolution_path, table_relationships 등이 추가된 결과 리스트
    (matched / auto_confirmed / inferred_confirmed 만 다음 단계로 넘어갈 대상, unresolved는 여기서 종결)
    """
    con = duckdb.connect(META_DB_PATH)
    con.execute("LOAD vss;")

    results = []
    for row in parsed_rows:
        eng_name = str(row["영문명"]).strip()
        exact = exact_match_meta_db(con, eng_name)

        if exact["found"]:
            out = {**row, "match_status": "matched", "meta_row": exact["meta_row"],
                   "match_evidence": "영문명 정확 매칭", "resolution_path": "validated"}
            results.append(_enrich_relationships(con, out))
            continue

        attempts = 0
        top_k, floor, glossary_boost = 5, SIMILARITY_PREFILTER_FLOOR, False
        resolved = False
        while not resolved:
            candidates = retrieve_candidates(
                con, eng_name, str(row["항목설명"]), embed_fn,
                top_k=top_k, floor=floor, include_glossary_boost=glossary_boost,
            )
            if not candidates:
                out = {**row, "match_status": "unresolved", "meta_row": None,
                       "match_evidence": "검색된 후보 없음", "unresolved_reason": "no_match",
                       "resolution_path": "no_match"}
                results.append(out)
                resolved = True
                break

            judgment = generate_match_judgment(row, candidates, chat_fn=chat_fn)
            route = decide_route(judgment, attempts)

            if route == "auto_confirm":
                selected = next(c for c in candidates if c["column_id"] == judgment["selected_column_id"])
                update_meta_tag(con, selected["column_id"], "auto_confirmed", confidence=judgment["confidence"])
                log_auto_confirm(selected["column_id"], eng_name, judgment["confidence"], judgment["evidence"])
                out = {**row, "match_status": "auto_confirmed", "meta_row": selected["meta_row"],
                       "match_evidence": judgment["evidence"], "llm_confidence": judgment["confidence"],
                       "llm_evidence": judgment["evidence"], "resolution_path": "auto_confirmed"}
                results.append(_enrich_relationships(con, out))
                resolved = True

            elif route == "retry":
                attempts += 1
                params = expand_retrieval_params(attempts)
                top_k, floor, glossary_boost = params["top_k"], params["floor"], params["include_glossary_boost"]
                # 루프 계속 (재검색)

            else:  # human_confirm
                decision = request_inferred_confirmation(row, candidates, judgment, confirm_fn)
                confirm_result = apply_confirmation_result(decision)
                target_column_id = judgment.get("selected_column_id") or (candidates[0]["column_id"] if candidates else None)
                if target_column_id:
                    log_confirmation_to_audit(
                        target_column_id,
                        question=f"{eng_name} -> LLM 추천(확신도 {judgment['confidence']}) 승인?",
                        answer=decision,
                    )
                if decision == "approved" and target_column_id:
                    selected = next((c for c in candidates if c["column_id"] == target_column_id), candidates[0])
                    update_meta_tag(con, selected["column_id"], "inferred_confirmed", confidence=judgment["confidence"])
                    out = {**row, "match_status": "inferred_confirmed", "meta_row": selected["meta_row"],
                           "match_evidence": judgment["evidence"], "llm_confidence": judgment["confidence"],
                           "llm_evidence": judgment["evidence"], "resolution_path": "validated"}
                    results.append(_enrich_relationships(con, out))
                else:
                    if target_column_id:
                        update_meta_tag(con, target_column_id, "unresolved", confidence=judgment["confidence"])
                    out = {**row, "match_status": "unresolved", "meta_row": None,
                           "match_evidence": "담당자 확인 결과 거절", "unresolved_reason": "rejected_by_human",
                           "resolution_path": "rejected_by_human"}
                    results.append(out)
                resolved = True

    con.close()
    return results


if __name__ == "__main__":
    import sys

    file_path = sys.argv[1] if len(sys.argv) > 1 else "./data/sample_spec.xlsx"

    from agents.parsing_agent import run_parsing
    from llm_client import embed

    parsed = run_parsing(file_path)
    meta_results = run_meta_search(parsed["parsed_rows"], embed)

    print("\n" + "=" * 60)
    print("[Meta Search 결과 요약]")
    print("=" * 60)
    counts = {}
    for r in meta_results:
        counts[r["match_status"]] = counts.get(r["match_status"], 0) + 1
    for status, cnt in counts.items():
        print(f"  {status}: {cnt}건")
