"""
Report Agent

역할: 전체 파이프라인 결과를 취합하여 최종 제공 가능 명세서(엑셀)로 재구성.
- 경로 A(정상 검증): Meta Search(matched/inferred_confirmed) -> DB Validation -> Classification
- 경로 B(담당자 거절/미매칭): Meta Search에서 unresolved로 조기 종결 (DB Validation/Classification 미경유)
두 경로를 resolution_path 컬럼으로 구분해 하나의 산출물로 통합.
"""

import os
import sys
import sqlite3
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")

RESOLUTION_PATH_LABEL = {
    "validated": "정상 검증",
    "rejected_by_human": "담당자 거절",
    "no_match": "매칭 후보 없음",
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
def aggregate_results(meta_results: list, classified_results: list) -> pd.DataFrame:
    rows = []

    # 경로 A: 정상 검증 (Classification까지 완료)
    for r in classified_results:
        rows.append({
            "영문명": r.get("영문명"),
            "한글명": r.get("한글명"),
            "요청 type": r.get("spec_type"),
            "제공가능 type": r.get("actual_type"),
            "요청시점(기간)": r.get("시점(기간)"),
            "제공가능시점(기간)": _fmt_period(r.get("final_period")),
            "항목설명": r.get("항목설명"),
            "소속테이블": r.get("source_table"),
            "최종태그": r.get("final_tag"),
            "근거": r.get("evidence"),
            "resolution_path": "validated",
        })

    # 경로 B: Meta Search에서 조기 종결 (unresolved - 거절 또는 미매칭)
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
        })

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
def generate_excel_report(merged_df: pd.DataFrame, output_path: str = "./output_명세서.xlsx"):
    df = merged_df.copy()
    df["태그설명"] = df.apply(_compute_tag_label, axis=1)
    df["처리경로"] = df["resolution_path"].map(RESOLUTION_PATH_LABEL).fillna(df["resolution_path"])

    ordered_cols = [
        "영문명", "한글명",
        "요청 type", "제공가능 type",
        "요청시점(기간)", "제공가능시점(기간)",
        "항목설명", "소속테이블",
        "최종태그", "태그설명", "처리경로", "근거",
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

    return output_path


# ── Tool: compute_summary_stats ────────────────────────────
def compute_summary_stats(merged_df: pd.DataFrame) -> dict:
    tag_counts = merged_df["최종태그"].value_counts().to_dict()
    path_counts = merged_df["resolution_path"].value_counts().to_dict()
    return {
        "total": len(merged_df),
        "tag_counts": tag_counts,
        "resolution_path_counts": path_counts,
    }


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
def run_report(meta_results: list, classified_results: list, input_file_path: str = None, output_path: str = None) -> dict:
    if output_path is None:
        output_path = _build_output_filename(input_file_path)

    merged_df = aggregate_results(meta_results, classified_results)
    excel_path = generate_excel_report(merged_df, output_path)
    stats = compute_summary_stats(merged_df)

    # 처리 완료 스냅샷 기록
    for _, row in merged_df.iterrows():
        log_revision_snapshot(
            column_id=row["영문명"],
            before_tag=None,
            after_tag=row["최종태그"],
            reason=f"resolution_path={row['resolution_path']}",
        )

    return {"excel_path": excel_path, "stats": stats}


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
    from llm_client import embed

    parsed = run_parsing(file_path)
    meta_results = run_meta_search(parsed["parsed_rows"], embed)
    validation_results = run_db_validation(meta_results)
    classified = run_classification(validation_results)

    result = run_report(meta_results, classified, input_file_path=file_path)

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