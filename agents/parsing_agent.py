"""
Parsing Agent

역할: 업로드된 엑셀 명세서를 읽어 영문명/한글명/항목설명/type/시점(기간)을 표준 스키마로 구조화.
- 원본 값 임의 수정·추론 금지
- 컬럼 누락 시 침묵하지 않고 반드시 파싱 실패 로그 기록
"""

import os
import re
import json
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
# 한 행에 최소 1개만 있어도 나머지를 메타DB/LLM으로 보완 시도하는 대상 필드
NAME_FIELDS = ["영문명", "한글명", "항목설명"]

# LLM 헤더 매핑 확신도 이 값 이상이면 담당자 확인 없이 자동 반영
LLM_MAPPING_CONFIDENCE = 0.70


_HEADER_ROW_SYSTEM_PROMPT = """너는 엑셀 시트에서 어느 행이 컬럼 헤더(제목) 행인지 판별하는 감사관이다.
아래 "행_목록"은 시트 앞부분 각 행의 셀 값을 순서대로 나열한 것이다(0번부터 시작).
데이터 값이 아니라 짧은 라벨/제목처럼 보이는 행을 헤더 행으로 판단한다.
반드시 다음 JSON 스키마로만 응답한다:
{
  "header_row_index": 행_목록 중 하나의 row_index(정수) 또는 null(헤더 행을 찾을 수 없음),
  "evidence": "40자 내외 근거"
}
다른 텍스트(설명, 코드블록 표시 등)는 절대 포함하지 마라."""


def _detect_header_row_with_llm(raw: pd.DataFrame, max_rows: int = 5, chat_fn=None) -> int:
    """규칙 매칭 실패 시(비표준 헤더명), 앞부분 행들을 LLM에 보여주고 헤더 행 위치를 판별."""
    if chat_fn is None:
        from llm_client import chat as chat_fn  # 지연 import (테스트에서 mock 주입 용이하게)

    scan_range = min(max_rows, len(raw))
    rows_payload = [
        {"row_index": i, "values": [str(v) for v in raw.iloc[i].tolist()]}
        for i in range(scan_range)
    ]

    resp = chat_fn(
        "DEPLOYMENT_GPT41_MINI",
        [
            {"role": "system", "content": _HEADER_ROW_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"행_목록": rows_payload}, ensure_ascii=False)},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    idx = parsed.get("header_row_index")
    return idx if isinstance(idx, int) and 0 <= idx < scan_range else None


# ── Tool 1: parse_excel_to_df ──────────────────────────────
def parse_excel_to_df(file_path: str, chat_fn=None) -> pd.DataFrame:
    """엑셀 파일을 DataFrame으로 로드, 헤더 자동 탐지(1~3행 내 키워드 매칭, 실패 시 LLM 판별)"""
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
        # 헤더명이 표준 키워드와 다른 비표준 포맷 -> LLM으로 헤더 행 판별
        header_row_idx = _detect_header_row_with_llm(raw, chat_fn=chat_fn)

    if header_row_idx is None:
        raise ValueError("헤더 행을 찾을 수 없습니다 (1~3행 내 표준 필드 키워드 미검출, LLM 판별도 실패)")

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


_HEADER_MAP_SYSTEM_PROMPT = """너는 통신사 데이터 명세서 엑셀의 원본 헤더를 표준 필드에 매핑하는 감사관이다.
아래 "표준_필드_목록"과 "실제_헤더_목록"(헤더명 + 샘플값)만 근거로 판단한다.
실제_헤더_목록에 없는 헤더명을 임의로 만들어내지 않는다.
반드시 다음 JSON 스키마로만 응답한다:
{
  "<표준_필드명>": {
    "column": "실제_헤더_목록 중 하나의 헤더명 또는 null(적합한 헤더 없음)",
    "confidence": 0.0~1.0,
    "evidence": "40자 내외, 왜 이 헤더를 선택했는지(또는 왜 못 골랐는지)"
  },
  ...
}
요청된 모든 표준_필드에 대해 빠짐없이 응답하라. 다른 텍스트(설명, 코드블록 표시 등)는 절대 포함하지 마라."""


# ── Tool: map_columns_with_llm (규칙 매칭 실패 시 LLM 폴백) ──
def map_columns_with_llm(df: pd.DataFrame, missing_fields: list, excluded_columns: set,
                          chat_fn=None) -> dict:
    """규칙 매칭으로 못 찾은 표준 필드를, 실제 헤더명+샘플값을 근거로 LLM이 매핑.
    반환: {표준필드: {"column": 원본컬럼명 또는 None, "confidence": float, "evidence": str}}
    """
    if chat_fn is None:
        from llm_client import chat as chat_fn  # 지연 import (테스트에서 mock 주입 용이하게)

    available_columns = [c for c in df.columns if c not in excluded_columns]
    header_samples = []
    for col in available_columns:
        non_null = df[col].dropna()
        sample = str(non_null.iloc[0]) if len(non_null) else None
        header_samples.append({"name": str(col), "sample": sample})

    payload = {
        "표준_필드_목록": [{"field": f, "keywords": STANDARD_FIELDS[f]} for f in missing_fields],
        "실제_헤더_목록": header_samples,
    }

    resp = chat_fn(
        "DEPLOYMENT_GPT41_MINI",
        [
            {"role": "system", "content": _HEADER_MAP_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {}

    available_names = {str(c) for c in available_columns}
    result = {}
    for f in missing_fields:
        entry = parsed.get(f) or {}
        column = entry.get("column")
        confidence = max(0.0, min(1.0, float(entry.get("confidence", 0.0)))) if entry else 0.0
        evidence = entry.get("evidence", "")
        if column is not None and str(column) not in available_names:
            # 실제 헤더 목록 밖 응답 -> hallucination으로 간주, 안전한 경로로 강제 폴백
            column = None
            confidence = 0.0
            evidence = (evidence + " (검증 실패: 실제 헤더 목록 밖 응답)").strip()
        result[f] = {"column": column, "confidence": confidence, "evidence": evidence}

    # 서로 다른 표준 필드가 같은 컬럼을 가리키면, 확신도가 낮은 쪽은 폐기(담당자 확인으로 넘김)
    by_column: dict = {}
    for f, entry in result.items():
        col = entry["column"]
        if col is None:
            continue
        by_column.setdefault(col, []).append(f)
    for col, fields in by_column.items():
        if len(fields) <= 1:
            continue
        fields.sort(key=lambda f: result[f]["confidence"], reverse=True)
        for loser in fields[1:]:
            result[loser] = {"column": None, "confidence": 0.0,
                              "evidence": f"'{col}'가 다른 표준 필드와 중복 매핑되어 폐기됨"}
    return result


# ── Tool: request_header_mapping_confirmation (담당자 확인) ─
def _console_header_confirm(payload: dict) -> dict:
    print("\n" + "=" * 60)
    print("[담당자 확인 요청 - 헤더 매핑 실패]")
    print(f"  표준 필드   : {payload['missing_std_field']}")
    for a in payload["attempts"]:
        print(f"  - {a['method']}: {a['detail']}")
    print("  원본 헤더   :")
    for h in payload["all_headers"]:
        mapped = f" (-> {h['mapped_to']})" if h["mapped_to"] else ""
        print(f"    {h['name']} [{h['sample']}]{mapped}")
    unmapped = [h["name"] for h in payload["all_headers"] if not h["mapped_to"]]
    ans = input(f"매핑할 헤더를 입력하세요 ({unmapped}) / 없으면 'reject': ").strip()
    if ans == "reject" or ans not in unmapped:
        return {"decision": "rejected"}
    return {"decision": "approved", "selected_column": ans}


def request_header_mapping_confirmation(missing_std_field: str, attempts: list, header_mapping: dict,
                                         df: pd.DataFrame, suggested_column, confirm_fn=None) -> dict:
    """규칙+LLM 매핑에 모두 실패한 표준 필드를 담당자가 원본 헤더 중에서 직접 고르게 한다.
    반환: {"decision": "approved"|"rejected", "selected_column": 원본컬럼명 또는 None}
    """
    mapped_columns = {v: k for k, v in header_mapping.items()}
    all_headers = []
    for col in df.columns:
        non_null = df[col].dropna()
        sample = str(non_null.iloc[0]) if len(non_null) else None
        all_headers.append({
            "name": str(col),
            "sample": sample,
            "mapped_to": mapped_columns.get(col),
        })

    payload = {
        "type": "header_mapping_confirmation",
        "missing_std_field": missing_std_field,
        "attempts": attempts,
        "all_headers": all_headers,
        "suggested_column": str(suggested_column) if suggested_column is not None else None,
    }

    fn = confirm_fn or _console_header_confirm
    raw = fn(payload)
    if isinstance(raw, dict):
        return {"decision": raw.get("decision"), "selected_column": raw.get("selected_column")}
    # confirm_fn이 단순 문자열만 반환하는 경우(과거 방식과의 호환)
    if raw == "approved":
        return {"decision": "approved", "selected_column": suggested_column}
    return {"decision": "rejected", "selected_column": None}


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


# ── Tool 5: find_row_completion (부분 정보 행 보완) ─────────
def _get_meta_search_tools() -> dict:
    """agents.meta_search_agent를 지연 import.
    단독 실행(python agents/parsing_agent.py) 시에도 패키지 경로가 잡히도록 project root를 sys.path에 보장."""
    import sys
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from agents.meta_search_agent import (
        META_DB_PATH, exact_match_meta_db, retrieve_candidates, SIMILARITY_PREFILTER_FLOOR,
    )
    return {
        "META_DB_PATH": META_DB_PATH,
        "exact_match_meta_db": exact_match_meta_db,
        "retrieve_candidates": retrieve_candidates,
        "SIMILARITY_PREFILTER_FLOOR": SIMILARITY_PREFILTER_FLOOR,
    }


def _open_meta_con():
    tools = _get_meta_search_tools()
    con = duckdb.connect(tools["META_DB_PATH"])
    try:
        con.execute("LOAD vss;")
    except duckdb.Error:
        pass
    return con


def _meta_description_parts(description) -> tuple:
    """메타DB의 description은 흔히 '한글명 | 설명' 형태로 저장됨(scripts/build_glossary_seed.py 참고)."""
    if not description:
        return None, None
    text = str(description)
    if " | " in text:
        kor, desc = text.split(" | ", 1)
        return kor.strip(), desc.strip()
    return None, text


_ROW_INFER_SYSTEM_PROMPT = """너는 통신사 데이터 컬럼 명세의 영문명/한글명/항목설명 간 관계를 잘 아는 전문가다.
아래 "알려진_정보"를 보고 "요청_필드" 각각에 대해 가장 그럴듯한 값을 추론한다.
확실하지 않으면 낮은 확신도(0.3 이하)로 응답하고, 전혀 짐작할 수 없으면 value를 null로 응답한다.
반드시 다음 JSON 스키마로만 응답한다:
{
  "<요청필드명>": {"value": "추론값 또는 null", "confidence": 0.0~1.0, "evidence": "40자 내외 근거"},
  ...
}
다른 텍스트(설명, 코드블록 표시 등)는 절대 포함하지 마라."""


def infer_missing_fields_with_llm(present_fields: dict, missing_fields: list, chat_fn=None) -> dict:
    """메타DB에 후보가 없을 때, LLM 자체 지식으로 누락 필드를 추론(참고용 - 반드시 담당자 확인 필요)."""
    if chat_fn is None:
        from llm_client import chat as chat_fn  # 지연 import (테스트에서 mock 주입 용이하게)

    payload = {"알려진_정보": present_fields, "요청_필드": missing_fields}
    resp = chat_fn(
        "DEPLOYMENT_GPT41_MINI",
        [
            {"role": "system", "content": _ROW_INFER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {}

    result = {}
    for f in missing_fields:
        entry = parsed.get(f) or {}
        confidence = max(0.0, min(1.0, float(entry.get("confidence", 0.0)))) if entry else 0.0
        result[f] = {"value": entry.get("value"), "confidence": confidence, "evidence": entry.get("evidence", "")}
    return result


def find_row_completion(row: dict, con, chat_fn=None, embed_fn=None) -> dict:
    """영문명/한글명/항목설명 중 일부만 있는 행에 대해, 메타DB 매칭을 우선 시도하고
    후보가 없으면 LLM 자체 지식으로 나머지를 추정한다(둘 다 담당자 확인 대상).
    반환: {missing_fields, proposed_fill, source, confidence, evidence} 또는 채울 근거가 전혀 없으면 None
    """
    present = {
        f: row[f] for f in NAME_FIELDS
        if row.get(f) is not None and str(row.get(f)).strip() not in ("", "nan")
    }
    missing = [f for f in NAME_FIELDS if f not in present]
    if not missing or not present:
        return None  # 이미 다 있거나(보완 불필요), 근거가 전혀 없음(추정 불가)

    tools = _get_meta_search_tools()
    eng_name = str(present.get("영문명", "")).strip()
    query_text = " ".join(str(v) for v in present.values())

    candidate_meta_row, match_confidence, match_evidence = None, 0.0, ""

    if eng_name:
        exact = tools["exact_match_meta_db"](con, eng_name)
        if exact["found"]:
            candidate_meta_row = exact["meta_row"]
            match_confidence, match_evidence = 1.0, "영문명 정확 매칭"

    if candidate_meta_row is None:
        if embed_fn is None:
            from llm_client import embed as embed_fn  # 지연 import (테스트에서 mock 주입 용이하게)
        candidates = tools["retrieve_candidates"](
            con, eng_name, query_text, embed_fn, top_k=1,
            floor=tools["SIMILARITY_PREFILTER_FLOOR"], include_glossary_boost=True,
        )
        if candidates:
            candidate_meta_row = candidates[0]["meta_row"]
            match_confidence = candidates[0]["score"]
            match_evidence = f"{candidates[0]['source']} 유사도 매칭"

    if candidate_meta_row is not None:
        kor_name, desc = _meta_description_parts(candidate_meta_row.get("description"))
        meta_values = {"영문명": candidate_meta_row.get("column_name"), "한글명": kor_name, "항목설명": desc}
        proposed = {f: meta_values[f] for f in missing if meta_values.get(f)}
        if proposed:
            return {"missing_fields": missing, "proposed_fill": proposed, "source": "meta_db",
                    "confidence": round(float(match_confidence), 4), "evidence": match_evidence}

    # 메타DB 후보 없음 -> LLM 자체 지식으로 추론
    llm_result = infer_missing_fields_with_llm(present, missing, chat_fn=chat_fn)
    proposed = {f: v["value"] for f, v in llm_result.items() if v.get("value")}
    if not proposed:
        return None
    confidences = [v["confidence"] for f, v in llm_result.items() if v.get("value")]
    evidence = "; ".join(f"{f}: {v['evidence']}" for f, v in llm_result.items() if v.get("value"))
    return {"missing_fields": missing, "proposed_fill": proposed, "source": "llm_inference",
            "confidence": round(sum(confidences) / len(confidences), 4), "evidence": evidence}


# ── Tool: request_row_completion_confirmation (담당자 일괄 확인) ─
def _console_row_completion_confirm(payload: dict) -> dict:
    print("\n" + "=" * 60)
    print("[담당자 확인 요청 - 부분 정보 행 보완]")
    for c in payload["candidates"]:
        print(f"  row {c['row_index']}: 누락 {c['missing_fields']} -> 제안 {c['proposed_fill']} "
              f"(출처: {c['source']}, 확신도 {c['confidence']:.2f}) - {c['evidence']}")
    ans = input("전체 승인 'all' / 전체 거절 'none' / 일부만 승인할 row_index를 쉼표로 입력: ").strip()
    if ans == "all":
        return {"decision": "approved", "approved_row_indices": [c["row_index"] for c in payload["candidates"]]}
    if not ans or ans == "none":
        return {"decision": "rejected", "approved_row_indices": []}
    try:
        indices = [int(x.strip()) for x in ans.split(",")]
    except ValueError:
        indices = []
    return {"decision": "approved", "approved_row_indices": indices}


def request_row_completion_confirmation(pending_completions: list, confirm_fn=None) -> dict:
    """영문명/한글명/항목설명 중 일부만 있던 행들의 보완안을, 파싱이 모두 끝난 뒤 한 번에 일괄 확인받는다.
    반환: {"decision": "approved"|"rejected", "approved_row_indices": [...]}
    """
    payload = {"type": "row_completion_confirmation", "candidates": pending_completions}
    fn = confirm_fn or _console_row_completion_confirm
    raw = fn(payload)
    if isinstance(raw, dict):
        return {"decision": raw.get("decision"), "approved_row_indices": raw.get("approved_row_indices") or []}
    # confirm_fn이 단순 문자열만 반환하는 경우(과거 방식과의 호환) - 승인 시 전체 반영
    if raw == "approved":
        return {"decision": "approved", "approved_row_indices": [c["row_index"] for c in pending_completions]}
    return {"decision": "rejected", "approved_row_indices": []}


# ── 오케스트레이션 (LangGraph 노드로 감싸기 전 단계) ────────
def run_parsing(file_path: str, confirm_fn=None, chat_fn=None) -> dict:
    """
    confirm_fn: 규칙+LLM 매핑에 모두 실패한 표준 필드를 담당자에게 확인받을 콜백.
                None이면 CLI(input()) 기반 콘솔 확인으로 폴백.
    chat_fn: LLM 헤더 매핑에 사용할 llm_client.chat 래퍼. None이면 llm_client.chat 사용.
    """
    state = {
        "raw_file": file_path,
        "parsed_rows": [],
        "failed_rows": [],
        "total_count": 0,
        "parse_success_rate": 0.0,
        "candidate_tables": {},
    }

    # Step 1: 파일 로드 + 규칙 기반 헤더 매핑
    df = parse_excel_to_df(file_path, chat_fn=chat_fn)
    header_mapping = map_columns_by_header(df)

    missing_std_fields = [f for f in REQUIRED_FIELDS if f not in header_mapping]

    # Step 1-b: 규칙 매칭 실패 필드는 LLM으로 재시도(비표준 헤더명 대응)
    unresolved_fields = []
    field_attempts: dict = {f: [{
        "method": "규칙 매칭",
        "detail": f"키워드 {STANDARD_FIELDS[f]} 매칭 컬럼 없음",
    }] for f in missing_std_fields}

    if missing_std_fields:
        excluded = set(header_mapping.values())
        llm_mapping = map_columns_with_llm(df, missing_std_fields, excluded, chat_fn=chat_fn)
        for f in missing_std_fields:
            guess = llm_mapping[f]
            field_attempts[f].append({
                "method": "LLM 매칭",
                "detail": f"추천: {guess['column']} (확신도 {guess['confidence']:.2f}) - {guess['evidence']}",
            })
            if guess["column"] is not None and guess["confidence"] >= LLM_MAPPING_CONFIDENCE:
                header_mapping[f] = guess["column"]
            else:
                unresolved_fields.append((f, guess["column"]))

    # Step 1-c: 그래도 못 찾은 필드는 담당자가 원본 헤더 중에서 직접 선택
    for f, suggested_column in unresolved_fields:
        decision = request_header_mapping_confirmation(
            f, field_attempts[f], header_mapping, df, suggested_column, confirm_fn=confirm_fn,
        )
        if decision["decision"] == "approved" and decision["selected_column"] is not None:
            header_mapping[f] = decision["selected_column"]

    still_missing = [f for f in REQUIRED_FIELDS if f not in header_mapping]
    if still_missing:
        raise ValueError(f"명세서에서 표준 필드를 찾을 수 없습니다: {still_missing}")

    # 파싱에 필요 없는 헤더(비고/작성자 등)는 버리고 매핑된 컬럼만 남긴다.
    # 매핑된 컬럼이 전부 비어있는 행(합계/공백 구분 행 등)도 함께 제거.
    mapped_cols = list(dict.fromkeys(header_mapping.values()))
    df = df[mapped_cols].dropna(how="all")
    state["total_count"] = len(df)

    def _finalize_row(idx, row: dict) -> None:
        validation = validate_row_schema(row)
        if not validation["valid"]:
            state["failed_rows"].append({
                "row_index": idx,
                "row_data": row,
                "missing_fields": validation["missing_fields"],
            })
            return
        # Step 4: 후보 테이블 매핑
        candidates = map_candidate_tables(str(row["영문명"]).strip())
        state["candidate_tables"][row["영문명"]] = candidates
        state["parsed_rows"].append(row)

    # Step 2~3: 행 단위 구조화 + 스키마 검증
    meta_con = None
    pending_completions = []
    rows_by_idx = {}

    for idx, raw_row in df.iterrows():
        row = {
            std_field: (None if pd.isna(raw_row[orig_col]) else raw_row[orig_col])
            for std_field, orig_col in header_mapping.items()
        }
        rows_by_idx[idx] = row

        present_name_fields = [
            f for f in NAME_FIELDS
            if row.get(f) is not None and str(row.get(f)).strip() not in ("", "nan")
        ]
        # 영문명/한글명/항목설명 중 일부만 있으면 메타DB/LLM으로 나머지 보완 시도(담당자 확인 후 반영)
        if present_name_fields and len(present_name_fields) < len(NAME_FIELDS):
            if meta_con is None:
                meta_con = _open_meta_con()
            completion = find_row_completion(row, meta_con, chat_fn=chat_fn)
            if completion:
                pending_completions.append({"row_index": idx, **completion})
                continue

        _finalize_row(idx, row)

    if meta_con is not None:
        meta_con.close()

    # Step 3-b: 보완 제안이 있으면 파싱 완료 후 한 번에 일괄 확인받아 반영
    if pending_completions:
        decision = request_row_completion_confirmation(pending_completions, confirm_fn=confirm_fn)
        approved_indices = set(decision.get("approved_row_indices") or [])
        for item in pending_completions:
            idx = item["row_index"]
            row = rows_by_idx[idx]
            if idx in approved_indices:
                row.update(item["proposed_fill"])
            _finalize_row(idx, row)

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