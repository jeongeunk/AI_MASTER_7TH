"""
Join Resolution Agent

역할: 명세서가 서로 다른 테이블의 컬럼을 동시에 요청할 때, 그 테이블들을 실제로
      하나의 결과셋으로 묶을 수 있는지(조인 가능성)를 검증한다.
      지금까지의 파이프라인(Meta Search -> DB Validation -> Classification)은 컬럼
      하나하나가 "존재하는가"만 보고, 서로 다른 테이블에서 온 컬럼들을 합칠 수
      있는지는 전혀 보지 않았다 - 이 Agent가 그 갭을 메운다.

동작 순서 (테이블 쌍마다):
1. table_relationships에 이미 등록된 관계가 있으면(직접 연결 또는 중간 테이블을
   경유하는 최대 2-hop 경로) 그대로 신뢰하고 사용한다. 재확인 없음.
2. 없으면 조인키 후보를 추정한다:
   a. 규칙: 두 테이블 스키마에 이름이 동일한 컬럼 (둘 다 월별 grain이면 month를
      묶어 복합키로 제안 - 안 그러면 fact-fact 조인 시 카티션 곱이 생김)
   b. 임베딩: column_embeddings 코사인 유사도로 이름이 다른 후보도 찾음
3. 후보는 반드시 실제 데이터로 검증한다 - 이름/설명이 그럴듯해도 값이 안 겹치면
   조인키가 아니다(예: total_ic_mou/total_og_mou는 임베딩 유사도 0.84로 높게
   나오지만 실제로는 둘 다 통화시간 "값" 컬럼이라 조인키가 될 수 없음).
   SEMI JOIN 기반 distinct value 포함률(containment)이 그 근거다.
4. 포함률이 최소 기준(JOIN_MIN_CONTAINMENT_FLOOR)도 못 넘으면 후보 자체가 없는
   것으로 보고 조인 불가로 종결한다. 넘으면 반드시 담당자 확인(HITL)을 거친다 -
   메타 매칭과 달리 조인키는 한 번 잘못 확정되면 이후 모든 명세서에 영향을 주므로
   자동 확정 경로를 두지 않는다.
5. 승인된 조인키는 table_relationships에 source='inferred_confirmed'로 저장해
   다음 명세서부터는 재추론 없이 재사용된다.
6. 최종 확정된(또는 기존에 있던) 조인키에 대해 관련된 모든 테이블의 컬럼 type과
   보유기간을 조회해 - type 불일치 여부와 "조인된 결과가 실제로 유효한 기간"
   (모든 관련 테이블 보유기간의 교집합)을 계산한다.

전체 테이블 쌍 처리가 끝나면 한 번 더:
7. "조인이 가능하다"와 "그 조인에 필요한 키가 요청 컬럼 목록에 실제로 포함돼 있다"는
   다른 문제다(find_missing_join_key_columns). 확정된 조인 경로가 쓰는 키 컬럼 중
   사용자가 요청하지 않은 게 있으면 담당자에게 알리고(request_missing_key_confirmation),
   승인 시 그 키를 다른 요청 컬럼과 동일한 형태로 meta_results에 추가한다
   (build_added_key_row) - 이후 DB Validation/Classification/Report가 그대로
   이어받아 검증·노출한다. run_join_resolution의 반환값은 이제
   {"join_results": [...], "meta_results": [...]}이며, 호출부는 반드시 갱신된
   meta_results를 다음 단계로 넘겨야 한다.
"""

import os
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.trace import tool_span
from agents.db_validation_agent import get_guarded_connection, query_column_type, query_retention_period, _safe_ident
from agents.classification_agent import intersect_periods

load_dotenv()

# 값 overlap이 이 미만이면 후보로도 취급하지 않는다 (담당자 확인조차 안 띄움 - 근거가 너무 약함)
JOIN_MIN_CONTAINMENT_FLOOR = 0.30
SEMANTIC_CANDIDATE_TOP_K = 3
SEMANTIC_CANDIDATE_FLOOR = 0.60


# ── Tool: _load_relationship_edges ─────────────────────────
def _load_relationship_edges(con) -> list:
    with tool_span("load_table_relationships"):
        rows = con.execute(
            "SELECT from_table_id, to_table_id, join_key, relation_type, source, confidence "
            "FROM table_relationships"
        ).fetchall()
    return [
        {"from": r[0], "to": r[1], "join_key": r[2], "relation_type": r[3], "source": r[4], "confidence": r[5]}
        for r in rows
    ]


# ── Tool: find_join_path (기존 관계 그래프 탐색, 최대 2-hop) ──
def find_join_path(edges: list, table_a: str, table_b: str, max_hops: int = 2, context: str = None):
    """table_relationships 그래프에서 table_a -> table_b 경로를 BFS로 찾는다.
    직접 연결이 없어도 공통 테이블(보통 dim 테이블)을 경유하는 경로를 찾을 수 있다.
    반환: hop 리스트(각 {from,to,join_key,relation_type,source,confidence}) 또는 못 찾으면 None."""
    with tool_span("find_join_path", context=context) as span:
        span.set_args({"table_a": table_a, "table_b": table_b})

        if table_a == table_b:
            span.set_result([])
            return []

        adjacency = {}
        for e in edges:
            adjacency.setdefault(e["from"], []).append(e)
            adjacency.setdefault(e["to"], []).append({**e, "from": e["to"], "to": e["from"]})

        queue = deque([(table_a, [])])
        visited = {table_a}
        while queue:
            node, path = queue.popleft()
            if len(path) >= max_hops:
                continue
            for edge in adjacency.get(node, []):
                nxt = edge["to"]
                if nxt in visited:
                    continue
                new_path = path + [edge]
                if nxt == table_b:
                    span.set_result(f"경로 발견({len(new_path)} hop)")
                    return new_path
                visited.add(nxt)
                queue.append((nxt, new_path))
        span.set_result("경로 없음")
        return None


# ── Tool: find_shared_identifier_columns (규칙 기반 후보) ───
def _has_literal_month_column(con, table: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM duckdb_columns() "
        "WHERE database_name = 'data_db' AND table_name = ? AND column_name = 'month'",
        [table],
    ).fetchone()[0] > 0


def find_shared_identifier_columns(con, table_a: str, table_b: str) -> list:
    """두 테이블 스키마에서 컬럼명이 정확히 같은 것을 찾는다 - 조인키 후보의 1순위."""
    with tool_span("find_shared_identifier_columns"):
        rows = con.execute(
            """
            SELECT ca.column_name, ca.data_type
            FROM duckdb_columns() ca
            JOIN duckdb_columns() cb
              ON ca.column_name = cb.column_name
             AND ca.database_name = 'data_db' AND cb.database_name = 'data_db'
             AND ca.table_name = ? AND cb.table_name = ?
            """,
            [table_a, table_b],
        ).fetchall()
    return [{"column": r[0], "data_type": r[1]} for r in rows]


def build_rule_based_candidates(con, table_a: str, table_b: str) -> list:
    shared = find_shared_identifier_columns(con, table_a, table_b)
    id_cols = [c["column"] for c in shared if c["column"].lower() != "month"]
    if not id_cols:
        return []

    # 두 테이블이 모두 월별 grain이면 식별자만으로는 부족하다 - 안 그러면
    # fact-fact 조인 시 고객당 N개월 x M개월 카티션 곱이 생긴다.
    both_monthly = _has_literal_month_column(con, table_a) and _has_literal_month_column(con, table_b)

    candidates = []
    for col in id_cols:
        if both_monthly:
            candidates.append({"cols_a": [col, "month"], "cols_b": [col, "month"], "source": "name_match_composite"})
        else:
            candidates.append({"cols_a": [col], "cols_b": [col], "source": "name_match"})
    return candidates


# ── Tool: find_semantic_key_candidates (임베딩 기반 후보) ───
def find_semantic_key_candidates(con, table_a: str, table_b: str,
                                  top_k: int = SEMANTIC_CANDIDATE_TOP_K,
                                  floor: float = SEMANTIC_CANDIDATE_FLOOR) -> list:
    """이름이 달라 규칙으로 못 찾은 경우, 컬럼 설명 임베딩 유사도로 후보를 찾는다.
    주의: 이름/설명이 비슷해도 실제 조인키가 아닐 수 있다(예: 두 테이블의 '총 통화시간'
    컬럼끼리는 의미상 유사해도 값 컬럼이라 조인키가 될 수 없음) - 그래서 여기서 나온
    후보는 반드시 check_value_overlap으로 실측 검증한 뒤에만 채택한다."""
    with tool_span("find_semantic_key_candidates", model="text-embedding-3-large(pre-computed)"):
        rows = con.execute(
            """
            SELECT a.column_name, b.column_name,
                   1 - array_cosine_distance(ea.embedding, eb.embedding) AS similarity
            FROM column_spec a
            JOIN column_embeddings ea ON ea.column_id = a.column_id
            JOIN column_spec b ON b.table_id = ?
            JOIN column_embeddings eb ON eb.column_id = b.column_id
            WHERE a.table_id = ?
              AND a.column_name != b.column_name
              AND a.data_type NOT IN ('DOUBLE', 'FLOAT')
              AND b.data_type NOT IN ('DOUBLE', 'FLOAT')
            ORDER BY similarity DESC
            LIMIT ?
            """,
            [table_b, table_a, top_k],
        ).fetchall()
    return [
        {"cols_a": [r[0]], "cols_b": [r[1]], "source": "embedding", "similarity": round(r[2], 4)}
        for r in rows if r[2] >= floor
    ]


# ── Tool: check_value_overlap (그라운드 트루스 검증) ────────
def check_value_overlap(con, table_a: str, cols_a: list, table_b: str, cols_b: list) -> dict:
    """후보 키의 실제 distinct 값이 두 테이블 사이에 얼마나 겹치는지 직접 측정한다.
    이름/임베딩 유사도는 후보를 좁히는 용도일 뿐, 조인키로 확정할 근거는 이 값
    overlap(포함률/containment)이어야 한다 - 이름이 같아도 값 도메인이 다르면
    조인이 안 되고, 반대로 값이 실제로 거의 다 겹치면 이름이 달라도 유효한 키다."""
    with tool_span("check_value_overlap"):
        safe_table_a, safe_table_b = _safe_ident(table_a), _safe_ident(table_b)
        safe_cols_a = [_safe_ident(c) for c in cols_a]
        safe_cols_b = [_safe_ident(c) for c in cols_b]
        select_a, select_b = ", ".join(safe_cols_a), ", ".join(safe_cols_b)
        not_null_a = " AND ".join(f"{c} IS NOT NULL" for c in safe_cols_a)
        not_null_b = " AND ".join(f"{c} IS NOT NULL" for c in safe_cols_b)

        row_count_a, distinct_a = con.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT ({select_a})) FROM data_db.{safe_table_a} WHERE {not_null_a}"
        ).fetchone()
        row_count_b, distinct_b = con.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT ({select_b})) FROM data_db.{safe_table_b} WHERE {not_null_b}"
        ).fetchone()

        on_clause = " AND ".join(f"a.{ca} = b.{cb}" for ca, cb in zip(safe_cols_a, safe_cols_b))
        overlap_count = con.execute(
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {select_a} FROM data_db.{safe_table_a} WHERE {not_null_a}) a "
            f"SEMI JOIN (SELECT DISTINCT {select_b} FROM data_db.{safe_table_b} WHERE {not_null_b}) b "
            f"ON {on_clause}"
        ).fetchone()[0]

    return {
        "row_count_a": row_count_a, "row_count_b": row_count_b,
        "distinct_a": distinct_a, "distinct_b": distinct_b,
        "overlap_count": overlap_count,
        "containment_a_in_b": round(overlap_count / distinct_a, 4) if distinct_a else 0.0,
        "containment_b_in_a": round(overlap_count / distinct_b, 4) if distinct_b else 0.0,
        "relation_type": f"{'1' if row_count_a == distinct_a else 'N'}:{'1' if row_count_b == distinct_b else 'N'}",
    }


def score_candidate(con, table_a: str, table_b: str, candidate: dict) -> dict:
    stats = check_value_overlap(con, table_a, candidate["cols_a"], table_b, candidate["cols_b"])
    confidence = max(stats["containment_a_in_b"], stats["containment_b_in_a"])
    return {**candidate, "overlap_stats": stats, "confidence": confidence}


def generate_join_key_candidates(con, table_a: str, table_b: str) -> list:
    """규칙(이름 일치) -> 임베딩 순으로 후보를 모으고, 전부 실측 값 overlap으로
    점수를 매겨 confidence 내림차순으로 반환한다."""
    candidates = build_rule_based_candidates(con, table_a, table_b)
    candidates += find_semantic_key_candidates(con, table_a, table_b)
    scored = [score_candidate(con, table_a, table_b, c) for c in candidates]
    scored.sort(key=lambda c: c["confidence"], reverse=True)
    return scored


def _describe_candidate(candidate: dict) -> str:
    stats = candidate["overlap_stats"]
    src_label = {
        "name_match": "컬럼명 일치",
        "name_match_composite": "컬럼명 일치 (월별 grain이라 month 포함 복합키로 제안)",
        "embedding": "임베딩 유사도",
    }
    label = src_label.get(candidate["source"], candidate["source"])
    return (
        f"{label} 후보. 실측 값 overlap: A→B 포함률 {stats['containment_a_in_b'] * 100:.1f}%, "
        f"B→A 포함률 {stats['containment_b_in_a'] * 100:.1f}% "
        f"(distinct A={stats['distinct_a']}, B={stats['distinct_b']}, 겹치는 값 {stats['overlap_count']}건)"
    )


# ── Tool: request_join_key_confirmation (담당자 확인) ──────
def _console_join_confirm(payload: dict) -> str:
    print("\n" + "=" * 60)
    print("[담당자 확인 요청 - 조인키 추정]")
    print(f"  테이블: {payload['table_a']} <-> {payload['table_b']}")
    print(f"  추정 키: {payload['join_key']}")
    print(f"  근거: {payload['evidence']}")
    print("=" * 60)
    ans = input("이 키로 조인을 확정하시겠습니까? (y/n): ").strip().lower()
    return "approved" if ans == "y" else "rejected"


def request_join_key_confirmation(table_a: str, table_b: str, candidate: dict, confirm_fn=None, context: str = None) -> str:
    """confirm_fn: payload(dict) -> "approved" | "rejected". None이면 콘솔 input().
    메타 매칭과 달리 조인키는 confidence가 아무리 높아도 자동 확정 경로를 두지 않는다 -
    한 번 잘못 확정되면 table_relationships에 저장돼 이후 모든 명세서에 영향을 준다."""
    payload = {
        "type": "join_key_confirmation",
        "table_a": table_a,
        "table_b": table_b,
        "join_key": ",".join(candidate["cols_a"]),
        "source": candidate["source"],
        "confidence": candidate["confidence"],
        "overlap_stats": candidate["overlap_stats"],
        "evidence": _describe_candidate(candidate),
    }
    fn = confirm_fn or _console_join_confirm
    with tool_span("request_join_key_confirmation (HITL)", context=context) as span:
        span.set_args(payload)
        decision = fn(payload)
        span.set_result(decision)
    return decision


# ── Tool: persist_confirmed_relationship ───────────────────
def persist_confirmed_relationship(con, table_a: str, table_b: str, join_key: str,
                                    relation_type: str, confidence: float, source: str) -> str:
    """확정된 조인키를 table_relationships에 저장해 다음 명세서부터 재사용되게 한다
    (meta_search_agent.update_meta_tag가 컬럼 매칭 결과를 캐싱하는 것과 같은 목적)."""
    relationship_id = f"rel_inferred__{table_a}__{table_b}__{join_key.replace(',', '_')}"
    with tool_span("persist_confirmed_relationship"):
        exists = con.execute(
            "SELECT 1 FROM table_relationships WHERE relationship_id = ?", [relationship_id]
        ).fetchone()
        if not exists:
            con.execute(
                "INSERT INTO table_relationships "
                "(relationship_id, from_table_id, to_table_id, join_key, relation_type, source, confidence) "
                "VALUES (?,?,?,?,?,?,?)",
                [relationship_id, table_a, table_b, join_key, relation_type, source, confidence],
            )
            con.commit()
    return relationship_id


# ── Tool: validate_join_path (type/보유기간 검증) ───────────
def _split_join_key(join_key: str) -> list:
    return [c.strip() for c in str(join_key).split(",") if c.strip()]


def validate_join_path(con, path: list, context: str = None) -> dict:
    """조인 경로에 관련된 모든 테이블에 대해, 조인키 컬럼(들)의 type이 일치하는지와
    각 테이블의 보유기간을 조회하고, 그 교집합(=조인된 결과가 실제로 유효한 기간)을
    구한다. db_validation_agent의 query_column_type/query_retention_period를 그대로
    재사용 - 조인키도 결국 일반 컬럼과 똑같이 검증해야 한다."""
    with tool_span("validate_join_path", context=context) as span:
        tables_involved = sorted({t for hop in path for t in (hop["from"], hop["to"])})
        key_parts = sorted({part for hop in path for part in _split_join_key(hop["join_key"])})
        span.set_args({"tables_involved": tables_involved, "key_parts": key_parts})

        key_types = {}
        types_by_col = {}
        for table in tables_involved:
            for col in key_parts:
                t = query_column_type(con, table, col)
                key_types[f"{table}.{col}"] = t
                types_by_col.setdefault(col, set()).add(t)
        type_consistent = all(len(s) <= 1 for s in types_by_col.values())

        period_by_table = {}
        for table in tables_involved:
            rp = query_retention_period(con, table, "", context=context)
            period_by_table[table] = (rp["start"], rp["end"])

        joined_period = intersect_periods(list(period_by_table.values()), context=context)

        result = {
            "tables_involved": tables_involved,
            "key_types": key_types,
            "type_consistent": type_consistent,
            "period_by_table": period_by_table,
            "joined_period": joined_period,
        }
        span.set_result({"type_consistent": type_consistent, "joined_period": joined_period})
    return result


# ── Tool: resolve_join_for_pair (테이블 쌍 하나 처리) ───────
def resolve_join_for_pair(con, table_a: str, table_b: str, edges: list, confirm_fn=None) -> dict:
    pair_context = f"{table_a}<->{table_b}"
    path = find_join_path(edges, table_a, table_b, context=pair_context)
    if path:
        validation = validate_join_path(con, path, context=pair_context)
        join_key_desc = " -> ".join(f"{h['from']}.{h['join_key']}={h['to']}.{h['join_key']}" for h in path)
        return {
            "table_a": table_a, "table_b": table_b, "status": "resolved", "source": "declared",
            "path": path, "join_key": join_key_desc,
            "confidence": min((h.get("confidence") or 1.0) for h in path),
            "evidence": "table_relationships에 등록된 관계" + ("을 경유해 연결" if len(path) > 1 else ""),
            **validation,
        }

    candidates = generate_join_key_candidates(con, table_a, table_b)
    viable = [c for c in candidates if c["confidence"] >= JOIN_MIN_CONTAINMENT_FLOOR]
    if not viable:
        reason = ("이름/의미상 후보는 있었으나 실제 값이 거의 겹치지 않아 조인 불가로 판단"
                   if candidates else "이름/의미상 조인 후보가 될 컬럼을 찾지 못함")
        return {"table_a": table_a, "table_b": table_b, "status": "join_key_missing",
                "source": None, "evidence": reason}

    best = viable[0]
    decision = request_join_key_confirmation(table_a, table_b, best, confirm_fn=confirm_fn, context=pair_context)
    if decision != "approved":
        return {
            "table_a": table_a, "table_b": table_b, "status": "join_key_missing",
            "source": "inferred_rejected",
            "evidence": f"담당자가 추정 조인키({','.join(best['cols_a'])})를 거절함",
            "candidate": best,
        }

    join_key = ",".join(best["cols_a"])
    persist_confirmed_relationship(
        con, table_a, table_b, join_key,
        relation_type=best["overlap_stats"]["relation_type"],
        confidence=best["confidence"], source="inferred_confirmed",
    )
    hop = {"from": table_a, "to": table_b, "join_key": join_key,
           "relation_type": best["overlap_stats"]["relation_type"],
           "source": "inferred_confirmed", "confidence": best["confidence"]}
    validation = validate_join_path(con, [hop], context=pair_context)
    return {
        "table_a": table_a, "table_b": table_b, "status": "resolved", "source": "inferred_confirmed",
        "path": [hop], "join_key": join_key, "confidence": best["confidence"],
        "evidence": _describe_candidate(best),
        **validation,
    }


# ── Tool: find_missing_join_key_columns (인도물 완결성 체크) ─
def find_missing_join_key_columns(join_results: list, meta_results: list) -> list:
    """조인이 '가능하다'는 것과 그 조인에 필요한 키가 '요청된 컬럼 목록에 실제로
    포함돼 있다'는 건 다른 문제다. 예를 들어 mobile_number 없이 circle_id/vol_3g_mb/
    total_ic_mou만 요청하면, 세 컬럼이 각각 제공 가능해도 받는 쪽은 이걸 한 행으로
    묶을 방법이 없다. 확정된(declared/inferred_confirmed) 조인 경로가 실제로 쓰는
    키 컬럼들을, 사용자가 이미 요청한 컬럼 이름 집합과 대조해 빠진 것을 찾는다.
    (컬럼명이 테이블 간에 같으면 같은 키로 취급하는 지금 설계와 일관되게, 어느
    테이블에서 요청했는지는 안 가리고 이름만으로 대조한다.)
    """
    with tool_span("find_missing_join_key_columns") as span:
        requested_columns = {
            r["meta_row"]["column_name"] for r in meta_results
            if r.get("match_status") in ("matched", "inferred_confirmed") and r.get("meta_row")
        }

        missing = {}  # key_column -> {"tables": set, "needed_for": set of (table_a, table_b)}
        for r in join_results:
            if r.get("status") != "resolved":
                continue
            for hop in r.get("path", []):
                for part in _split_join_key(hop["join_key"]):
                    if part in requested_columns:
                        continue
                    entry = missing.setdefault(part, {"tables": set(), "needed_for": set()})
                    entry["tables"].add(hop["from"])
                    entry["tables"].add(hop["to"])
                    entry["needed_for"].add((r["table_a"], r["table_b"]))

        result = [
            {"key_column": col, "tables": sorted(info["tables"]), "needed_for": sorted(info["needed_for"])}
            for col, info in missing.items()
        ]
        span.set_result(f"누락 키 {len(result)}건: {[r['key_column'] for r in result]}")
    return result


# ── Tool: request_missing_key_confirmation (담당자 확인) ───
def _console_missing_key_confirm(payload: dict) -> str:
    print("\n" + "=" * 60)
    print("[담당자 확인 요청 - 조인키가 요청 목록에 없음]")
    print(f"  키 컬럼   : {payload['key_column']}")
    print(f"  관련 테이블: {payload['tables']}")
    print(f"  이 키 없이는 못 묶는 쌍: {payload['needed_for']}")
    print(f"  근거      : {payload['evidence']}")
    print("=" * 60)
    ans = input("이 키 컬럼을 요청 목록에 추가하시겠습니까? (y/n): ").strip().lower()
    return "approved" if ans == "y" else "rejected"


def request_missing_key_confirmation(missing_key: dict, confirm_fn=None) -> str:
    """confirm_fn: payload(dict) -> "approved" | "rejected". None이면 콘솔 input()."""
    pairs = ", ".join(f"{a}<->{b}" for a, b in missing_key["needed_for"])
    payload = {
        "type": "missing_join_key_confirmation",
        "key_column": missing_key["key_column"],
        "tables": missing_key["tables"],
        "needed_for": missing_key["needed_for"],
        "evidence": (
            f"'{missing_key['key_column']}' 컬럼이 요청 목록에 없어서, 조인이 가능하다고 확인된 "
            f"{pairs}의 결과를 실제로 한 행으로 묶을 방법이 없습니다."
        ),
    }
    fn = confirm_fn or _console_missing_key_confirm
    with tool_span("request_missing_key_confirmation (HITL)", context=missing_key.get("key_column")) as span:
        span.set_args(payload)
        decision = fn(payload)
        span.set_result(decision)
    return decision


# ── Tool: build_added_key_row (승인된 키를 요청 컬럼으로 합성) ─
def _pick_source_table_for_key(tables: list) -> str:
    """키 컬럼을 어느 테이블 소속으로 표시할지 - dim 테이블이 있으면 그게 통상
    이 식별자의 원천(canonical source)이므로 우선한다."""
    dim_tables = [t for t in tables if "dim_" in t]
    return sorted(dim_tables)[0] if dim_tables else sorted(tables)[0]


def _lookup_meta_row(con, table: str, column: str) -> dict:
    row = con.execute(
        "SELECT column_id, table_id, column_name, data_type, description "
        "FROM column_spec WHERE table_id = ? AND column_name = ?",
        [table, column],
    ).fetchone()
    if not row:
        return None
    return {"column_id": row[0], "table_id": row[1], "column_name": row[2], "data_type": row[3], "description": row[4]}


def build_added_key_row(con, missing_key: dict) -> dict:
    """승인된 조인키 컬럼을, 다른 요청 컬럼과 완전히 동일한 형태의 meta_results 행으로
    합성한다 - 그래야 DB Validation(존재/type/보유기간) -> Classification -> Report를
    다른 컬럼과 똑같이 통과해 최종 리포트에 정상적인 한 행으로 나온다."""
    table = _pick_source_table_for_key(missing_key["tables"])
    meta_row = _lookup_meta_row(con, table, missing_key["key_column"])
    if meta_row is None:
        return None
    return {
        "영문명": meta_row["column_name"],
        "한글명": "조인키(자동 추가)",
        "항목설명": meta_row.get("description"),
        "type": None,
        "시점(기간)": None,
        "match_status": "matched",
        "meta_row": meta_row,
        "match_evidence": (
            f"조인키 자동 추가 - {', '.join(f'{a}<->{b}' for a, b in missing_key['needed_for'])} "
            f"조인 결과를 실제로 묶으려면 필요해 담당자 승인 후 요청 목록에 추가됨"
        ),
        "resolution_path": "join_key_added",
    }


# ── Tool: _resolve_pair_with_own_connection (병렬 워커용) ───
def _resolve_pair_with_own_connection(table_a: str, table_b: str, edges: list, confirm_fn) -> dict:
    """이미 등록된 관계로 담당자 확인 없이 확정되는(declared) 쌍 전용 워커.
    메인 스레드와 커넥션을 공유하면 DuckDB 커넥션 동시 접근 문제가 생기므로
    워커마다 자기 커넥션을 새로 연다. declared 쌍만 이 경로를 타므로
    confirm_fn(HITL)이 실제로 호출될 일은 없다(호출되면 안전을 위해 그대로 전달)."""
    con = get_guarded_connection()
    try:
        return resolve_join_for_pair(con, table_a, table_b, edges, confirm_fn=confirm_fn)
    finally:
        con.close()


# ── 오케스트레이션 ───────────────────────────────────────────
def run_join_resolution(meta_results: list, confirm_fn=None) -> dict:
    """
    meta_results: meta_search_agent.run_meta_search(...) 결과.
    matched/inferred_confirmed 행들의 source_table을 모아 distinct
    테이블이 2개 이상이면, 모든 테이블 쌍에 대해 조인 가능성을 검증한다.
    테이블이 1개 이하면(단일 테이블 명세서) 조인이 필요 없으므로 빈 결과 반환.

    Parallelization: 테이블 쌍마다 서로 독립적이지만, 새로 추정한 조인키는
    담당자 확인(interrupt())을 거쳐야 해서 무분별하게 병렬화하면 여러 스레드가
    동시에 확인을 요청하는 문제가 생긴다. 그래서 "이미 등록된 관계로 확정되는
    (declared, 담당자 확인 불필요) 쌍"만 스레드풀로 병렬 처리하고, "새로 추정해야
    하는(inferred, 확인이 필요할 수 있는) 쌍"은 기존처럼 순차 처리한다. 이 데이터셋
    구조상 대부분의 쌍이 declared라 실질적인 처리시간 단축 효과가 크다.

    반환: {"join_results": [...], "meta_results": [...]}
    meta_results는 입력을 그대로 돌려주는 게 기본이지만, 조인에 필요한데 요청 목록에
    없던 키 컬럼이 있고 담당자가 추가를 승인하면 그 컬럼이 새 행으로 추가된 버전을
    돌려준다 - 이후 DB Validation/Classification/Report는 반드시 이 반환값의
    meta_results를 이어받아야 추가된 키도 같이 검증·노출된다.
    """
    tables = sorted({
        r["meta_row"]["table_id"] for r in meta_results
        if r.get("match_status") in ("matched", "inferred_confirmed") and r.get("meta_row")
    })
    if len(tables) < 2:
        return {"join_results": [], "meta_results": meta_results}

    con = get_guarded_connection()
    con.execute("LOAD vss;")
    edges = _load_relationship_edges(con)

    all_pairs = list(combinations(tables, 2))
    declared_pairs, inferred_pairs = [], []
    for table_a, table_b in all_pairs:
        if find_join_path(edges, table_a, table_b) is not None:
            declared_pairs.append((table_a, table_b))
        else:
            inferred_pairs.append((table_a, table_b))

    results_by_pair = {}

    if declared_pairs:
        with tool_span(f"resolve_declared_pairs (병렬 {len(declared_pairs)}건)"):
            with ThreadPoolExecutor(max_workers=min(8, len(declared_pairs))) as executor:
                futures = {
                    executor.submit(_resolve_pair_with_own_connection, a, b, edges, confirm_fn): (a, b)
                    for a, b in declared_pairs
                }
                for future in as_completed(futures):
                    results_by_pair[futures[future]] = future.result()

    for table_a, table_b in inferred_pairs:
        results_by_pair[(table_a, table_b)] = resolve_join_for_pair(
            con, table_a, table_b, edges, confirm_fn=confirm_fn
        )

    # 병렬 실행은 완료 순서가 뒤섞이므로, 매번 리포트 순서가 바뀌지 않도록
    # 원래 combinations() 순서(테이블명 사전순 쌍)로 재정렬한다.
    join_results = [results_by_pair[pair] for pair in all_pairs]

    updated_meta_results = list(meta_results)
    missing_keys = find_missing_join_key_columns(join_results, meta_results)
    for mk in missing_keys:
        decision = request_missing_key_confirmation(mk, confirm_fn=confirm_fn)
        if decision == "approved":
            added_row = build_added_key_row(con, mk)
            if added_row:
                updated_meta_results.append(added_row)

    con.close()
    return {"join_results": join_results, "meta_results": updated_meta_results}


if __name__ == "__main__":
    import sys

    from agents.parsing_agent import run_parsing
    from agents.meta_search_agent import run_meta_search
    from llm_client import embed

    file_path = sys.argv[1] if len(sys.argv) > 1 else "./data/sample_spec.xlsx"
    parsed = run_parsing(file_path)
    meta_results = run_meta_search(parsed["parsed_rows"], embed)
    result = run_join_resolution(meta_results)
    join_results = result["join_results"]

    print("\n" + "=" * 60)
    print("[Join Resolution 결과]")
    print("=" * 60)
    if not join_results:
        print("  단일 테이블 명세서이거나 조인 검증 대상 없음")
    for r in join_results:
        print(f"\n  {r['table_a']} <-> {r['table_b']}")
        print(f"    상태     : {r['status']} (source={r.get('source')})")
        print(f"    조인키   : {r.get('join_key')}")
        print(f"    신뢰도   : {r.get('confidence')}")
        print(f"    타입일치 : {r.get('type_consistent')}")
        print(f"    조인유효기간: {r.get('joined_period')}")
        print(f"    근거     : {r.get('evidence')}")

    added = [r for r in result["meta_results"] if r.get("resolution_path") == "join_key_added"]
    if added:
        print(f"\n  [조인을 위해 자동 추가된 컬럼] {len(added)}건")
        for r in added:
            print(f"    - {r['영문명']} ({r['meta_row']['table_id']})")
