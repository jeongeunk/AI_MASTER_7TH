"""
Classification Agent

역할: DB Validation Agent를 거쳐온 컬럼(matched/inferred_confirmed 경로)에 한해
      존재→type→기간 순서로 최종 태깅.
- type 비교는 DB Validation Agent에서 이미 확정되었으므로 재판단하지 않고 그대로 반영
- 기간 비교(full_period/confirm_period)는 담당자 확인 없이 자동 처리
- 최종 태그는 메타 DB column_spec.tag / confidence에 갱신 (Meta Search Agent와 공유하는 update_meta_tag 사용)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
from dotenv import load_dotenv
from agents.meta_search_agent import update_meta_tag  # 공유 도구 재사용

load_dotenv()

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")


# ── Tool: classify_existence ────────────────────────────────
def classify_existence(exists: bool) -> dict:
    if not exists:
        return {"tag": "not_found"}
    return {"tag": None}


# ── Tool: tag_from_type_status ──────────────────────────────
def tag_from_type_status(type_match_status: str) -> dict:
    """
    DB Validation Agent가 전달한 결과를 그대로 반영, 재비교 없음.
    - mismatch_confirmed(담당자가 DB 기준으로 갱신 승인) -> type 문제 해소된 것으로 보고 기간 비교로 진행
    - mismatch_rejected(담당자가 갱신 거절, 명세 type 유지) -> type_mismatch로 확정, 기간 비교 없이 종료
    - matched -> 기간 비교로 진행
    """
    if type_match_status == "mismatch_rejected":
        return {"tag": "type_mismatch"}
    return {"tag": None}


# ── Tool: compare_period ────────────────────────────────────
def _parse_period(period_str: str):
    """'202406~202412' -> (202406, 202412)"""
    parts = str(period_str).replace(" ", "").split("~")
    if len(parts) != 2:
        return None, None
    return int(parts[0]), int(parts[1])


def compare_period(requested: tuple, retained: tuple) -> dict:
    req_start, req_end = requested
    ret_start, ret_end = retained

    if ret_start is None or ret_end is None:
        return {"tag": "period_mismatch", "final_period": None,
                "evidence": "보유기간 정보 없음"}

    # 요청 기간 미기재 -> 현재 보유 중인 전체 기간을 참고로 제공
    if req_start is None or req_end is None:
        return {"tag": "full_period", "final_period": (ret_start, ret_end),
                "evidence": f"요청기간 미기재 - 현재 보유 중인 전체 기간({ret_start}~{ret_end})을 참고로 제공"}

    overlap_start = max(req_start, ret_start)
    overlap_end = min(req_end, ret_end)

    if overlap_start > overlap_end:
        return {"tag": "period_mismatch", "final_period": None,
                "evidence": f"요청기간({req_start}~{req_end})과 보유기간({ret_start}~{ret_end}) 겹치는 구간 없음"}

    if req_start >= ret_start and req_end <= ret_end:
        return {"tag": "full_period", "final_period": (req_start, req_end),
                "evidence": f"요청기간 전체가 보유기간({ret_start}~{ret_end})에 포함됨"}

    return {"tag": "confirm_period", "final_period": (overlap_start, overlap_end),
            "evidence": f"요청기간 일부만 보유 -> 제공가능 기간({overlap_start}~{overlap_end})으로 조정"}


# ── 오케스트레이션 ───────────────────────────────────────────
def run_classification(validation_results: list) -> list:
    """
    validation_results: db_validation_agent.run_db_validation(...) 결과
    """
    con = duckdb.connect(META_DB_PATH)
    results = []

    for row in validation_results:
        out = dict(row)
        column_id = row["meta_row"]["column_id"] if row.get("meta_row") else None

        # Step 1: 존재 여부
        existence = classify_existence(row["exists"])
        if existence["tag"] == "not_found":
            out["final_tag"] = "not_found"
            out["evidence"] = "메타에는 있었으나 실 DB에서 컬럼 확인 불가"
            out["final_period"] = None
            if column_id:
                update_meta_tag(con, column_id, "not_found", confidence=1.0)
            results.append(out)
            continue

        # Step 2: type 결과 반영 (재비교 없음) - mismatch_rejected만 여기서 종료
        type_tag = tag_from_type_status(row.get("type_match_status"))
        if type_tag["tag"] == "type_mismatch":
            out["final_tag"] = "type_mismatch"
            out["evidence"] = "명세 type과 실제 type 불일치 - 담당자 확인(거절)으로 명세 type 유지, 최종 확정"
            out["final_period"] = None
            if column_id:
                update_meta_tag(con, column_id, "type_mismatch", confidence=1.0)
            results.append(out)
            continue

        # Step 3: 기간 비교 (type matched 이거나, mismatch_confirmed로 승인되어 실제 type으로 갱신된 경우 모두 여기로 진행)
        requested = _parse_period(row.get("시점(기간)"))
        retention = row.get("retention_period") or {}
        retained = (retention.get("start"), retention.get("end"))

        period_result = compare_period(requested, retained)
        out["final_tag"] = period_result["tag"]
        out["final_period"] = period_result["final_period"]
        out["retention_estimated"] = bool(retention.get("estimated"))

        evidence_prefix = ""
        if retention.get("estimated"):
            cols = ", ".join(retention.get("estimated_from") or [])
            evidence_prefix = f"[보유기간 추정: month 컬럼 없어 {cols} 값으로 추정] "

        if row.get("type_match_status") == "mismatch_confirmed":
            out["evidence"] = (
                f"[type 갱신 승인: {row.get('actual_type')}로 확정] "
                + evidence_prefix + period_result["evidence"]
            )
        else:
            out["evidence"] = evidence_prefix + period_result["evidence"]

        out["source_table"] = row.get("source_table")

        if column_id:
            update_meta_tag(con, column_id, period_result["tag"], confidence=1.0)

        results.append(out)

    con.close()
    return results


if __name__ == "__main__":
    import sys as _sys

    file_path = _sys.argv[1] if len(_sys.argv) > 1 else "./data/sample_spec.xlsx"

    from agents.parsing_agent import run_parsing
    from agents.meta_search_agent import run_meta_search
    from agents.db_validation_agent import run_db_validation
    from llm_client import embed

    parsed = run_parsing(file_path)
    meta_results = run_meta_search(parsed["parsed_rows"], embed)
    validation_results = run_db_validation(meta_results)
    classified = run_classification(validation_results)

    print("\n" + "=" * 60)
    print("[Classification 결과 요약]")
    print("=" * 60)
    counts = {}
    for r in classified:
        counts[r["final_tag"]] = counts.get(r["final_tag"], 0) + 1
    for tag, cnt in counts.items():
        print(f"  {tag}: {cnt}건")

    print("\n[상세]")
    for r in classified:
        print(f"  {r['영문명']:25s} -> {r['final_tag']:15s} | {r['evidence']}")