"""
Parsing Agent

역할: 업로드된 엑셀 명세서를 읽어 영문명/한글명/항목설명/type/시점(기간)을 표준 스키마로 구조화.
- 원본 값 임의 수정·추론 금지
- 컬럼 누락 시 침묵하지 않고 반드시 파싱 실패 로그 기록
"""

import os
import re
import duckdb
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

DATA_DB_PATH = os.environ.get("DATA_DB_PATH", "./db/schemascout_data.duckdb")

# 표준 필드 <- 실제 헤더에 포함될 수 있는 키워드
STANDARD_FIELDS = {
    "영문명": ["영문", "english", "eng_name", "column_name"],
    "한글명": ["한글", "korean", "kor_name", "국문"],
    "항목설명": ["설명", "description", "항목설명", "desc"],
    "type": ["type", "타입", "자료형", "데이터타입"],
    "시점(기간)": ["기간", "시점", "period", "요구기간"],
}
# 시점(기간)은 필수 아님 - 미기재 시 보유 기간 전체를 참고로 제공(Classification Agent에서 처리)
REQUIRED_FIELDS = ["영문명", "한글명", "항목설명", "type"]


# ── Tool 1: parse_excel_to_df ──────────────────────────────
def parse_excel_to_df(file_path: str) -> pd.DataFrame:
    """엑셀 파일을 DataFrame으로 로드, 헤더 자동 탐지(1~3행 내 키워드 매칭)"""
    raw = pd.read_excel(file_path, header=None)

    header_row_idx = None
    for i in range(min(3, len(raw))):
        row_values = [str(v) for v in raw.iloc[i].tolist()]
        row_text = " ".join(row_values)
        hits = sum(
            1
            for keywords in STANDARD_FIELDS.values()
            if any(kw in row_text for kw in keywords)
        )
        if hits >= 3:  # 표준 필드 키워드가 3개 이상 매칭되면 헤더 행으로 판단
            header_row_idx = i
            break

    if header_row_idx is None:
        raise ValueError("헤더 행을 찾을 수 없습니다 (1~3행 내 표준 필드 키워드 미검출)")

    df = pd.read_excel(file_path, header=header_row_idx)
    df = df.dropna(how="all")  # 완전 빈 행 제거
    return df


# ── Tool 2: map_columns_by_header ──────────────────────────
def map_columns_by_header(df: pd.DataFrame, header_row: int = 0) -> dict:
    """실제 헤더명을 표준 필드로 매핑 -> {표준필드: 원본컬럼명}"""
    mapping = {}
    for col in df.columns:
        col_str = str(col).strip().lower()
        for std_field, keywords in STANDARD_FIELDS.items():
            if std_field in mapping:
                continue
            if any(kw.lower() in col_str for kw in keywords):
                mapping[std_field] = col
                break
    return mapping


# ── Tool 3: validate_row_schema ────────────────────────────
def validate_row_schema(row: dict) -> dict:
    """행별 필수 필드 존재/공백 여부 검증 -> {valid, missing_fields}"""
    missing = [
        f for f in REQUIRED_FIELDS
        if f not in row or row[f] is None or str(row[f]).strip() in ("", "nan")
    ]
    return {"valid": len(missing) == 0, "missing_fields": missing}


# ── Tool 4: map_candidate_tables ───────────────────────────
_data_catalog_cache = None


def _load_data_catalog():
    global _data_catalog_cache
    if _data_catalog_cache is None:
        con = duckdb.connect(DATA_DB_PATH, read_only=True)
        rows = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
        ).fetchall()
        con.close()
        cache = {}
        for table_name, column_name in rows:
            cache.setdefault(column_name, []).append(table_name)
        _data_catalog_cache = cache
    return _data_catalog_cache


def map_candidate_tables(column_name: str) -> list:
    """컬럼명을 실데이터 DB 카탈로그(information_schema.columns) 기준 후보 테이블과 매핑"""
    catalog = _load_data_catalog()
    return catalog.get(column_name, [])


# ── 오케스트레이션 (LangGraph 노드로 감싸기 전 단계) ────────
def run_parsing(file_path: str) -> dict:
    state = {
        "raw_file": file_path,
        "parsed_rows": [],
        "failed_rows": [],
        "total_count": 0,
        "parse_success_rate": 0.0,
        "candidate_tables": {},
    }

    # Step 1: 파일 로드 + 헤더 탐지
    df = parse_excel_to_df(file_path)
    header_mapping = map_columns_by_header(df)

    missing_std_fields = [f for f in REQUIRED_FIELDS if f not in header_mapping]
    if missing_std_fields:
        raise ValueError(f"명세서에서 표준 필드를 찾을 수 없습니다: {missing_std_fields}")

    state["total_count"] = len(df)

    # Step 2~3: 행 단위 구조화 + 스키마 검증
    for idx, raw_row in df.iterrows():
        row = {
            std_field: (None if pd.isna(raw_row[orig_col]) else raw_row[orig_col])
            for std_field, orig_col in header_mapping.items()
        }
        validation = validate_row_schema(row)

        if not validation["valid"]:
            state["failed_rows"].append({
                "row_index": idx,
                "row_data": row,
                "missing_fields": validation["missing_fields"],
            })
            continue

        # Step 4: 후보 테이블 매핑
        candidates = map_candidate_tables(str(row["영문명"]).strip())
        state["candidate_tables"][row["영문명"]] = candidates
        state["parsed_rows"].append(row)

    state["parse_success_rate"] = (
        len(state["parsed_rows"]) / state["total_count"] if state["total_count"] else 0.0
    )
    return state


if __name__ == "__main__":
    import sys
    import json

    file_path = sys.argv[1] if len(sys.argv) > 1 else "./data/sample_spec.xlsx"
    result = run_parsing(file_path)

    print(f"[총 행 수] {result['total_count']}")
    print(f"[파싱 성공] {len(result['parsed_rows'])}건 ({result['parse_success_rate']*100:.1f}%)")
    print(f"[파싱 실패] {len(result['failed_rows'])}건")
    for f in result["failed_rows"]:
        print(f"   - row {f['row_index']}: 누락 필드 {f['missing_fields']}")

    print("\n[후보 테이블 매핑 샘플 5건]")
    for i, (col, tables) in enumerate(result["candidate_tables"].items()):
        if i >= 5:
            break
        print(f"   - {col}: {tables if tables else '(후보 없음 - not_found 가능성)'}")