"""
Report Agent

역할: 전체 파이프라인 결과를 취합하여 최종 제공 가능 명세서(엑셀)로 재구성.
- 경로 A(정상 검증): Meta Search(matched/inferred_confirmed) -> DB Validation -> Classification
  (추정 매칭은 confidence 크기와 무관하게 항상 담당자 확인을 거친 뒤에만 이 경로로 들어온다 -
   자동 확정 경로는 없다)
- 경로 B(담당자 거절/미매칭): Meta Search에서 unresolved로 조기 종결 (DB Validation/Classification 미경유)
"""

import os
import sys
import sqlite3
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.trace import tool_span, instrument_agent

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")

RESOLUTION_PATH_LABEL = {
    "validated": "정상 검증",
    "rejected_by_human": "담당자 거절",
    "no_match": "매칭 후보 없음",
    "join_key_added": "조인을 위해 자동 추가",
}

TAG_LABEL = {
    "not_found": "실 DB에 없음",
    "type_mismatch": "타입 불일치",
    "period_mismatch": "제공가능 기간 없음",
    "full_period": "요청기간 전체 제공가능",
    "confirm_period": "일부기간만 제공가능(조정됨)",
    "unresolved": "명세 매칭 실패",
}


# ── Tool: aggregate_results ────────────────────────────────
def _table_dot_column(source_table, meta_row: dict) -> str:
    """"소속테이블"에 테이블명뿐 아니라 실제 매칭된 컬럼명까지 "테이블.컬럼" 형태로 보여준다."""
    column_name = (meta_row or {}).get("column_name")
    if source_table and column_name:
        return f"{source_table}.{column_name}"
    return source_table


def aggregate_results(meta_results: list, classified_results: list) -> pd.DataFrame:
    rows = []

    # 경로 A: 정상 검증 (Classification까지 완료) — join_key_added 여부는 resolution_path로 구분
    for r in classified_results:
        resolution_path = (
            r.get("resolution_path")
            if r.get("resolution_path") == "join_key_added"
            else "validated"
        )
        evidence = r.get("evidence")
        if resolution_path == "join_key_added" and r.get("match_evidence"):
            evidence = f"[조인키 자동 추가] {r.get('match_evidence')} | {evidence}"

        rows.append({
            "영문명": r.get("영문명"),
            "한글명": r.get("한글명"),
            # "요청 type"은 명세서에 사용자가 실제로 적어 넣은 값 그대로(예: 수치형/범주형)를
            # 보여준다. spec_type(메타DB에 등록된 실제 컬럼 타입, 예: BIGINT/DOUBLE)은
            # DB 검증 단계의 내부 비교용일 뿐 사용자가 요청한 값이 아니므로 여기 노출하지 않는다.
            "요청 type": r.get("type"),
            "제공가능 type": r.get("actual_type"),
            "요청시점(기간)": r.get("시점(기간)"),
            "제공가능시점(기간)": _fmt_period(r.get("final_period")),
            "항목설명": r.get("항목설명"),
            "소속테이블": _table_dot_column(r.get("source_table"), r.get("meta_row")),
            "최종태그": r.get("final_tag"),
            "근거": evidence,
            "resolution_path": resolution_path,
            "재검색 횟수": r.get("retrieval_attempts"),
            "row_index": r.get("row_index"),  # 최종 정렬용(사용자가 업로드한 명세서 행 순서) - 출력 컬럼 아님
        })

    # 경로 B/C: Meta Search에서 조기 종결 (unresolved - 거절 또는 미매칭)
    for r in meta_results:
        if r.get("match_status") != "unresolved":
            continue
        rows.append({
            "영문명": r.get("영문명"),
            "한글명": r.get("한글명"),
            "요청 type": r.get("type"),  # 명세서 원본 type 필드 (DB 매칭 자체가 안 됐으므로 참고용)
            "제공가능 type": None,
            "요청시점(기간)": r.get("시점(기간)"),
            "제공가능시점(기간)": None,
            "항목설명": r.get("항목설명"),
            "소속테이블": None,
            "최종태그": "unresolved",
            "근거": r.get("match_evidence"),
            "resolution_path": r.get("unresolved_reason", "no_match"),
            "재검색 횟수": r.get("retrieval_attempts"),
            "row_index": r.get("row_index"),
        })

    # 사용자가 업로드한 명세서에 나열된 순서 그대로 최종 명세서를 만든다. row_index가
    # 없는 행(예: Join Resolution이 조인을 위해 원래 요청에 없던 키 컬럼을 자동으로
    # 추가한 경우 - join_key_added)은 원본 명세서에 없던 행이므로 맨 뒤로 보낸다.
    rows.sort(key=lambda r: r["row_index"] if r["row_index"] is not None else float("inf"))

    return pd.DataFrame(rows)


# ── Tool: build_join_report (Join Resolution Agent 결과 취합) ─
JOIN_SOURCE_LABEL = {
    "declared": "기존 등록된 관계",
    "inferred_confirmed": "AI 추정 + 담당자 확인",
    "inferred_rejected": "AI 추정 - 담당자 거절",
}


def build_join_report(join_results: list) -> pd.DataFrame:
    """명세서가 여러 테이블을 동시에 요청할 때, 그 테이블들을 실제로 하나의
    결과셋으로 조인할 수 있는지를 별도 시트로 보여준다. 컬럼별 '제공가능' 판정과
    달리, 여기서는 두 컬럼을 각각 받을 수 있어도 합쳐서 못 뽑는 경우를 잡아낸다."""
    with tool_span("build_join_report") as span:
        span.set_args({"join_results_count": len(join_results or [])})
        rows = []
        for r in join_results or []:
            if r["status"] == "resolved":
                period = r.get("joined_period")
                rows.append({
                    "테이블A": r["table_a"],
                    "테이블B": r["table_b"],
                    "조인키": r.get("join_key"),
                    "판정": "조인 가능",
                    "근거출처": JOIN_SOURCE_LABEL.get(r["source"], r["source"]),
                    "신뢰도": r.get("confidence"),
                    "타입일치": "일치" if r.get("type_consistent") else "불일치",
                    "조인가능기간(교집합)": _fmt_period(period) if period else "산출 불가(보유기간 정보 없음)",
                    "근거": r.get("evidence"),
                })
            else:
                rows.append({
                    "테이블A": r["table_a"],
                    "테이블B": r["table_b"],
                    "조인키": None,
                    "판정": "조인 불가",
                    "근거출처": JOIN_SOURCE_LABEL.get(r.get("source"), r.get("source") or "-"),
                    "신뢰도": None,
                    "타입일치": None,
                    "조인가능기간(교집합)": None,
                    "근거": r.get("evidence"),
                })
        span.set_result(f"조인 결과 {len(rows)}건 (가능 {sum(1 for r in rows if r['판정'] == '조인 가능')}건)")
    return pd.DataFrame(rows)


def _fmt_period(period):
    if not period:
        return None
    return f"{period[0]}~{period[1]}"


def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def _compute_tag_label(row) -> str:
    tag = row["최종태그"]
    requested = row.get("요청시점(기간)")

    if tag == "full_period" and _is_blank(requested):
        return "제공가능시점(기간) 전체 제공 가능"

    return TAG_LABEL.get(tag, tag)


# ── Tool: generate_excel_report ────────────────────────────
def generate_excel_report(merged_df: pd.DataFrame, output_path: str = "./output_명세서.xlsx",
                           join_df: pd.DataFrame = None):
    with tool_span("generate_excel_report (xlsxwriter)"):
        df = merged_df.copy()
        df["태그설명"] = df.apply(_compute_tag_label, axis=1)
        df["처리경로"] = df["resolution_path"].map(RESOLUTION_PATH_LABEL).fillna(df["resolution_path"])

        ordered_cols = [
            "영문명", "한글명",
            "요청 type", "제공가능 type",
            "요청시점(기간)", "제공가능시점(기간)",
            "항목설명", "소속테이블",
            "최종태그", "태그설명", "처리경로", "재검색 횟수", "근거",
        ]
        df = df[ordered_cols]

        with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="명세서 검증 결과")
            workbook = writer.book
            worksheet = writer.sheets["명세서 검증 결과"]
            header_fmt = workbook.add_format({"bold": True, "bg_color": "#EEEDFE", "border": 1})
            for col_idx, col_name in enumerate(df.columns):
                worksheet.write(0, col_idx, col_name, header_fmt)
                worksheet.set_column(col_idx, col_idx, max(14, len(col_name) + 4))

            # 명세서가 2개 이상 테이블을 요청한 경우에만 존재 - 컬럼별 판정과 별개로
            # "이 테이블들을 실제로 합쳐서 뽑을 수 있는가"를 보여주는 시트
            if join_df is not None and not join_df.empty:
                join_df.to_excel(writer, index=False, sheet_name="조인 가능성 검증")
                join_sheet = writer.sheets["조인 가능성 검증"]
                for col_idx, col_name in enumerate(join_df.columns):
                    join_sheet.write(0, col_idx, col_name, header_fmt)
                    join_sheet.set_column(col_idx, col_idx, max(14, len(str(col_name)) + 4))

    return output_path


# ── Tool: compute_summary_stats ────────────────────────────
def compute_summary_stats(merged_df: pd.DataFrame) -> dict:
    with tool_span("compute_summary_stats") as span:
        tag_counts = merged_df["최종태그"].value_counts().to_dict()
        path_counts = merged_df["resolution_path"].value_counts().to_dict()
        result = {
            "total": len(merged_df),
            "tag_counts": tag_counts,
            "resolution_path_counts": path_counts,
        }
        span.set_result(result)
    return result


# ── Tool: log_revision_snapshot ────────────────────────────
def log_revision_snapshot(column_id: str, before_tag: str, after_tag: str, reason: str = ""):
    con = sqlite3.connect(AUDIT_DB_PATH)
    con.execute(
        "INSERT INTO revision_history (column_id, before_tag, after_tag, reason) VALUES (?,?,?,?)",
        [column_id, before_tag, after_tag, reason],
    )
    con.commit()
    con.close()


# ── 오케스트레이션 ───────────────────────────────────────────
@instrument_agent("Report Agent")
def run_report(meta_results: list, classified_results: list, join_results: list = None,
                input_file_path: str = None, output_path: str = None) -> dict:
    if output_path is None:
        output_path = _build_output_filename(input_file_path)

    with tool_span("aggregate_results"):
        merged_df = aggregate_results(meta_results, classified_results)
        join_df = build_join_report(join_results)
    excel_path = generate_excel_report(merged_df, output_path, join_df=join_df)
    stats = compute_summary_stats(merged_df)
    if join_results:
        stats["join_summary"] = {
            "pairs_checked": len(join_results),
            "joinable": sum(1 for r in join_results if r["status"] == "resolved"),
            "not_joinable": sum(1 for r in join_results if r["status"] != "resolved"),
        }

    # 처리 완료 스냅샷 기록
    with tool_span(f"log_revision_snapshot ({len(merged_df)}건)"):
        for _, row in merged_df.iterrows():
            log_revision_snapshot(
                column_id=row["영문명"],
                before_tag=None,
                after_tag=row["최종태그"],
                reason=f"resolution_path={row['resolution_path']}",
            )

    return {
        "excel_path": excel_path,
        "stats": stats,
        # KPI2(자동 판별 커버리지) 계측용 - 이미 계산된 최종태그를 그대로 재사용
        # (metrics_store.py에서 같은 로직을 다시 만들지 않도록 여기서 흘려보냄)
        "rows": merged_df[["영문명", "최종태그", "resolution_path"]].to_dict("records"),
    }


def _build_output_filename(input_file_path: str) -> str:
    """
    'a.xlsx' -> 'a(output)_YYYYMMDD_HHMI.xlsx' (입력 파일과 같은 폴더에 생성)
    input_file_path가 없으면 './output' 기준으로 생성
    """
    now_str = datetime.now().strftime("%Y%m%d_%H%M")
    if not input_file_path:
        return f"./output(output)_{now_str}.xlsx"

    directory = os.path.dirname(input_file_path)
    base_name = os.path.splitext(os.path.basename(input_file_path))[0]
    filename = f"{base_name}(output)_{now_str}.xlsx"
    return os.path.join(directory, filename) if directory else filename


if __name__ == "__main__":
    file_path = sys.argv[1] if len(sys.argv) > 1 else "./data/sample_spec.xlsx"

    from agents.parsing_agent import run_parsing
    from agents.meta_search_agent import run_meta_search
    from agents.db_validation_agent import run_db_validation
    from agents.classification_agent import run_classification
    from agents.join_resolution_agent import run_join_resolution
    from llm_client import embed

    parsed = run_parsing(file_path)
    meta_results = run_meta_search(parsed["parsed_rows"], embed)
    join_result = run_join_resolution(meta_results)
    join_results = join_result["join_results"]
    meta_results = join_result["meta_results"]  # 조인키가 자동 추가됐을 수 있으므로 갱신본 사용
    validation_results = run_db_validation(meta_results)
    classified = run_classification(validation_results)

    result = run_report(meta_results, classified, join_results=join_results, input_file_path=file_path)

    print("\n" + "=" * 60)
    print("[Report Agent 결과]")
    print("=" * 60)
    print(f"산출물: {result['excel_path']}")
    print(f"\n총 {result['stats']['total']}건")
    print("\n[태그별 건수]")
    for tag, cnt in result["stats"]["tag_counts"].items():
        print(f"  {tag}: {cnt}건")
    print("\n[처리 경로별 건수]")
    for path, cnt in result["stats"]["resolution_path_counts"].items():
        print(f"  {path}: {cnt}건")
