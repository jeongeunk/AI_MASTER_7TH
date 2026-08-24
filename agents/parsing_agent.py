"""
agents/parsing_agent.py

역할: 업로드된 엑셀 명세서를 읽어 영문명/한글명/항목설명/type/시점(기간)을 표준 스키마로 구조화.
- 원본 값 임의 수정·추론 금지
- 컬럼 누락 시 침묵하지 않고 반드시 파싱 실패 로그 기록

[개정 이력]
- 헤더 "행" 탐지: 더 이상 1~3행으로 제한하지 않고 시트 전체를 스캔한다.
  확실한 후보(hits>=3)가 정확히 1개면 규칙만으로 확정하고(LLM 호출 없음),
  후보가 0개 또는 2개 이상으로 애매할 때만 LLM(gpt-4.1-mini)이 후보 중에서 판단한다
  (select_header_row_with_llm). 그래도 못 찾으면 시트 미리보기를 보여주고 담당자가
  직접 헤더 행 번호를 고르게 한다(header_row_confirmation).
- 헤더 "필드" 매핑: 규칙 기반 매핑(map_columns_by_header)만으로 표준 필드를 못 찾을 경우,
  헤더명+샘플값을 근거로 LLM(gpt-4.1-mini) fallback을 거쳐 재시도한다(generate_header_mapping_judgment).
  그래도 못 찾으면 기존처럼 ValueError로 파일 전체를 중단시키지 않고,
  confirm_fn(콘솔 input() 또는 LangGraph interrupt())을 통해 담당자가
  직접 원본 헤더 중 하나를 골라 확정할 수 있게 라우팅한다(header_mapping_confirmation).
- 행 단위 보완: 영문명/한글명/항목설명 중 1개 이상만 있으면(전부는 아님) 즉시 실패 처리하지 않고,
  메타DB(schemascout_meta.duckdb) 매칭을 우선 시도하고 후보가 없으면 LLM 추론으로 나머지를 채운다.
  제안값은 파싱이 모두 끝난 뒤 한 번에(row_completion_confirmation) 담당자 확인을 받는다.
- type은 필수 필드가 아니다. 행에 type이 없으면 실패 처리하지 않고 빈 채로 다음 Agent(DB Validation)에
  넘겨 실 DB 기준으로 처리하게 한다(시점(기간)과 동일한 취급).
"""

import json
import os
import re
import sys
import duckdb
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.meta_search_agent import exact_match_meta_db, retrieve_candidates, SIMILARITY_PREFILTER_FLOOR
from agents.trace import tool_span, instrument_agent

load_dotenv()

DATA_DB_PATH = os.environ.get("DATA_DB_PATH", "./db/schemascout_data.duckdb")
META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")

# 표준 필드 <- 실제 헤더에 포함될 수 있는 키워드
STANDARD_FIELDS = {
    "영문명": ["영문", "english", "eng_name", "column_name"],
    "한글명": ["한글", "korean", "kor_name", "국문"],
    "항목설명": ["설명", "description", "항목설명", "desc"],
    "type": ["type", "타입", "자료형", "데이터타입"],
    "시점(기간)": ["기간", "시점", "period", "요구기간"],
}
# 시점(기간)은 필수 아님 - 미기재 시 보유 기간 전체를 참고로 제공(Classification Agent에서 처리)
# type도 필수 아님 - 행에 없으면 빈 채로 다음 Agent(DB Validation)로 넘겨 실 DB 기준으로 처리
REQUIRED_FIELDS = ["영문명", "한글명", "항목설명"]
# 한 행에 최소 1개만 있어도 나머지를 메타DB/LLM으로 보완 시도하는 대상 필드
NAME_FIELDS = ["영문명", "한글명", "항목설명"]
# parsed_rows 출력 시 고정 필드 순서 - type/시점(기간)은 없으면 빈 값(None)으로 채워서 항상 이 순서로 노출
OUTPUT_FIELD_ORDER = ["영문명", "한글명", "항목설명", "type", "시점(기간)"]

# LLM 헤더 매핑 fallback 확신도 임계값 (실측 전 초기값 - 추후 조정 예정)
HEADER_LLM_CONFIDENCE_FLOOR = 0.75


# ── Tool 1: parse_excel_to_df ──────────────────────────────
# 헤더 위치에 행 제한을 두지 않는다(설명/요약 섹션이 위에 몇 행이든 올 수 있음).
# 대신 "규칙만으로 후보가 명확한 경우"와 "애매한 경우"를 나눠서,
# 애매할 때만 LLM을 부른다(5.3절 "예외 케이스에서만 호출" 설계 원칙 유지).
HEADER_SCAN_MAX_ROWS = 200  # 시트가 극단적으로 길 때의 비용 상한(대부분의 명세서는 이 안에 헤더가 있음)


def _find_header_row_candidates(raw: pd.DataFrame, min_hits: int = 3) -> list:
    """시트 전체(최대 HEADER_SCAN_MAX_ROWS행)를 스캔해 hits>=min_hits인 행을 전부 수집.
    첫 번째에서 멈추지 않고 전부 모아서, 후보가 1개인지 여러 개인지로 분기한다."""
    candidates = []
    scan_range = min(len(raw), HEADER_SCAN_MAX_ROWS)
    for i in range(scan_range):
        row_values = [str(v) for v in raw.iloc[i].tolist()]
        row_text = " ".join(row_values)
        hits = sum(
            1
            for keywords in STANDARD_FIELDS.values()
            if any(kw in row_text for kw in keywords)
        )
        if hits >= min_hits:
            candidates.append({"row_idx": i, "hits": hits, "preview": row_values})
    return candidates


def select_header_row_with_llm(candidates: list) -> tuple:
    """
    후보 헤더 행이 0개 또는 2개 이상으로 애매할 때, 후보 행들의 내용을 LLM(gpt-4.1-mini)에게
    보여주고 실제 명세 헤더 행이 어느 것인지 판단시킨다.
    candidates 목록 밖의 행 번호를 응답하지 못하도록 프롬프트로 제약(hallucination 방지).
    반환: (선택된 row_idx 또는 None, evidence)
    """
    if not candidates:
        return None, "후보 행이 전혀 없음"

    from llm_client import chat

    candidate_lines = "\n".join(
        f"- row_idx={c['row_idx']}: {c['preview']}" for c in candidates
    )
    system_prompt = (
        "너는 데이터 명세서 파일에서 실제 컬럼 헤더가 있는 행을 찾는 보조자다.\n"
        "아래 \"후보 행 목록\"만 근거로 판단한다. 목록에 없는 row_idx를 임의로 만들어내지 않는다.\n"
        "후보 행 목록은 표준 필드(영문명/한글명/항목설명/type/시점) 키워드가 일부 포함된 행들이며,\n"
        "이 중 설명/요약 문구가 아니라 실제 표의 컬럼 헤더 역할을 하는 행 하나를 고른다.\n"
        "반드시 다음 JSON 스키마로만 응답한다:\n"
        '{"header_row_idx": 후보 목록 중 하나의 row_idx(정수) 또는 null(적합한 행 없음), '
        '"evidence": "40자 내외, 왜 이 행을 선택했는지(또는 왜 못 골랐는지)"}'
    )
    user_prompt = f"후보 행 목록:\n{candidate_lines}"

    with tool_span("select_header_row_with_llm", model="gpt-4.1-mini"):
        response = chat(
            "DEPLOYMENT_GPT41_MINI",
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    parsed = json.loads(response.choices[0].message.content)
    selected = parsed.get("header_row_idx")
    evidence = parsed.get("evidence", "")

    valid_indices = {c["row_idx"] for c in candidates}
    if selected in valid_indices:
        return selected, evidence
    return None, evidence


# ── 담당자 확인 (헤더 "행" 판별 실패 시) ─────────────────────
def _console_header_row_confirm(payload: dict) -> dict:
    print("\n" + "=" * 60)
    print("[담당자 확인 요청 - 헤더 행 판별 실패]")
    for a in payload["attempts"]:
        print(f"  - {a['method']}: {a['detail']}")
    print("  원본 시트 미리보기:")
    for r in payload["row_previews"]:
        print(f"    [{r['row_idx']}] {r['preview']}")
    ans = input("헤더 행 번호를 입력하세요 (없으면 그냥 엔터): ").strip()
    if not ans:
        return {"decision": "rejected"}
    return {"decision": "approved", "selected_row_idx": int(ans)}


def request_header_row_confirmation(raw: pd.DataFrame, attempts: list, confirm_fn=None) -> dict:
    """규칙+LLM으로도 헤더 행을 못 찾았을 때, 담당자가 시트 미리보기에서 직접 행 번호를 고르게 한다.
    반환: {"decision": "approved"|"rejected", "selected_row_idx": int 또는 None}
    """
    scan_range = min(len(raw), 10)
    row_previews = [
        {"row_idx": i, "preview": [str(v) for v in raw.iloc[i].tolist()]}
        for i in range(scan_range)
    ]
    payload = {
        "type": "header_row_confirmation",
        "attempts": attempts,
        "row_previews": row_previews,
    }
    fn = confirm_fn or _console_header_row_confirm
    raw_decision = fn(payload)
    if isinstance(raw_decision, dict):
        return {"decision": raw_decision.get("decision"), "selected_row_idx": raw_decision.get("selected_row_idx")}
    # confirm_fn이 단순 문자열만 반환하는 경우 - 근거(선택된 행)가 없으므로 거절 처리
    return {"decision": "rejected", "selected_row_idx": None}


def parse_excel_to_df(file_path: str, confirm_fn=None) -> pd.DataFrame:
    """엑셀 파일을 DataFrame으로 로드, 헤더 자동 탐지(행 위치 제한 없이 시트 전체 스캔)"""
    with tool_span("read_excel"):
        raw = pd.read_excel(file_path, header=None)

    # 1차: 확실한 기준(hits>=3)으로 후보 수집
    with tool_span("rule_scan_header_row"):
        strong_candidates = _find_header_row_candidates(raw, min_hits=3)

    if len(strong_candidates) == 1:
        # 후보가 정확히 1개면 규칙만으로 확정 (LLM 호출 없음)
        header_row_idx = strong_candidates[0]["row_idx"]
    else:
        # 후보가 0개(기준을 못 넘음) 또는 2개 이상(애매함)이면 LLM에게 판단을 맡김.
        # 0개인 경우 기준을 완화(hits>=2)해 판단 대상 후보를 넓혀준다.
        candidates_for_llm = strong_candidates if strong_candidates else _find_header_row_candidates(raw, min_hits=2)
        header_row_idx, llm_evidence = select_header_row_with_llm(candidates_for_llm)

        if header_row_idx is None:
            # 규칙+LLM 모두 실패 -> 시트 미리보기를 보여주고 담당자가 직접 헤더 행을 고르게 함
            attempts = [
                {"method": "규칙 스캔", "detail": f"표준 필드 키워드 3개 이상 매칭 행 {len(strong_candidates)}개"},
                {"method": "LLM 판단 (gpt-4.1-mini)", "detail": llm_evidence or "적합한 헤더 행을 찾지 못함"},
            ]
            decision = request_header_row_confirmation(raw, attempts, confirm_fn=confirm_fn)
            if decision["decision"] == "approved" and decision.get("selected_row_idx") is not None:
                header_row_idx = decision["selected_row_idx"]
            else:
                raise ValueError(
                    f"헤더 행을 찾을 수 없습니다 (규칙 후보 {len(strong_candidates)}개, "
                    f"LLM 판단·담당자 확인 모두 실패)"
                )

    df = pd.read_excel(file_path, header=header_row_idx)
    df = df.dropna(how="all")  # 완전 빈 행 제거
    return df


# ── Tool 2: map_columns_by_header (규칙 기반, 기존 유지) ───
def map_columns_by_header(df: pd.DataFrame, header_row: int = 0) -> dict:
    """실제 헤더명을 표준 필드로 매핑 -> {표준필드: 원본컬럼명} (키워드 포함 매칭)"""
    with tool_span("map_columns_by_header"):
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


# ── Tool 2.5 (신규): generate_header_mapping_judgment ──────
def generate_header_mapping_judgment(missing_std_fields: list, df: pd.DataFrame, unmapped_columns: list) -> list:
    """
    규칙 매칭 실패한 표준 필드에 한해, 아직 배정되지 않은 원본 헤더 후보 중
    최적 매칭을 LLM(gpt-4.1-mini)으로 판단.
    헤더명뿐 아니라 실제 셀 샘플값도 함께 보여준다 — "필드코드" 같은 헤더는 이름만으로는
    영문명 여부를 알 수 없고 값이 'cust_id' 같은 영문 식별자인지 봐야 판단 가능하기 때문.
    후보(unmapped_columns) 밖의 컬럼명을 응답하지 못하도록 프롬프트로 제약(hallucination 방지).

    입력: missing_std_fields(list[str]), df(원본 DataFrame, 샘플값 추출용), unmapped_columns(list[str])
    출력: [{std_field, matched_column|null, confidence, evidence}]
    """
    if not missing_std_fields or not unmapped_columns:
        return []

    from llm_client import chat  # 지연 임포트 (단독 테스트 시 llm_client 없이도 규칙 매칭만 쓸 수 있게)

    header_samples = []
    for col in unmapped_columns:
        non_null = df[col].dropna()
        samples = [str(v) for v in non_null.head(3).tolist()]
        header_samples.append({"name": col, "samples": samples})

    system_prompt = (
        "너는 데이터 명세서 헤더를 표준 필드로 매핑하는 보조자다.\n"
        "아래 \"매핑이 필요한 표준 필드 목록\"과 \"아직 매핑되지 않은 원본 헤더 목록\"(헤더명 + 실제 셀 샘플값)만 "
        "근거로 판단한다.\n"
        "헤더명 자체가 애매해도 샘플값의 패턴(예: 영문 snake_case 식별자, 한글 명칭, 날짜 형식 등)을 "
        "적극 활용해 판단한다. 예를 들어 헤더명이 '필드코드'라도 샘플값이 'cust_id', 'cust_name'처럼 "
        "영문 식별자라면 영문명일 가능성이 높다.\n"
        "원본 헤더 목록에 없는 컬럼명을 임의로 만들어내지 않는다.\n"
        "하나의 원본 헤더는 하나의 표준 필드에만 매핑한다.\n"
        "반드시 다음 JSON 스키마로만 응답한다:\n"
        '{"mappings": [{"std_field": "표준 필드명", "matched_column": "원본 헤더 목록 중 하나 또는 null(적합한 헤더 없음)", '
        '"confidence": 0.0, "evidence": "40자 내외, 왜 이 헤더를 선택했는지(또는 왜 못 찾았는지, 샘플값 근거 포함)"}]}'
    )
    user_prompt = (
        f"매핑이 필요한 표준 필드 목록: {missing_std_fields}\n"
        f"아직 매핑되지 않은 원본 헤더 목록(헤더명 + 샘플값): {json.dumps(header_samples, ensure_ascii=False)}"
    )

    with tool_span("generate_header_mapping_judgment", model="gpt-4.1-mini"):
        response = chat(
            "DEPLOYMENT_GPT41_MINI",
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    parsed = json.loads(response.choices[0].message.content)
    return parsed.get("mappings", [])


# ── JSON 안전 변환 (pandas 결측치 NaN -> None) ─────────────
def _json_safe(value):
    """pandas가 빈 셀을 NaN(float)으로 읽는데, 표준 JSON은 NaN을 허용하지 않아
    FastAPI 응답 직렬화 시 500 에러가 난다. 행 데이터를 만드는 시점에 미리 정리한다."""
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


# ── Tool 3: validate_row_schema ────────────────────────────
def validate_row_schema(row: dict) -> dict:
    """행별 필수 필드 존재/공백 여부 검증 -> {valid, missing_fields}
    영문명/한글명/항목설명 중 최소 1개만 있으면 유효 처리 -> Meta Search Agent가 있는 정보로 매칭을
    시도할 수 있게 한다. 셋 다 없는 행만 무효(파싱 실패) 처리."""
    missing = [
        f for f in NAME_FIELDS
        if f not in row or row[f] is None or str(row[f]).strip() in ("", "nan")
    ]
    return {"valid": len(missing) < len(NAME_FIELDS), "missing_fields": missing}


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
    with tool_span("map_candidate_tables"):
        catalog = _load_data_catalog()
        return catalog.get(column_name, [])


# ── Tool 4.5: find_row_completion (부분 정보 행 보완) ──────
def _meta_description_parts(description) -> tuple:
    """메타DB의 description은 흔히 '한글명 | 설명' 형태로 저장됨(scripts/build_glossary_seed.py 참고)."""
    if not description:
        return None, None
    text = str(description)
    if " | " in text:
        kor, desc = text.split(" | ", 1)
        return kor.strip(), desc.strip()
    return None, text


def infer_name_fields_with_llm(present_fields: dict, missing_fields: list) -> dict:
    """메타DB에 후보가 없을 때, LLM 자체 지식으로 누락된 영문명/한글명/항목설명을 추론(참고용).
    반환: {필드명: {"value": 추론값 또는 None, "confidence": float, "evidence": str}}
    """
    from llm_client import chat  # 지연 임포트 (단독 테스트 시 llm_client 없이도 규칙 매칭만 쓸 수 있게)

    system_prompt = (
        "너는 통신사 데이터 컬럼 명세의 영문명/한글명/항목설명 간 관계를 잘 아는 전문가다.\n"
        "아래 \"알려진 정보\"를 보고 \"요청 필드\" 각각에 대해 가장 그럴듯한 값을 추론한다.\n"
        "확실하지 않으면 낮은 확신도(0.3 이하)로 응답하고, 전혀 짐작할 수 없으면 value를 null로 응답한다.\n"
        "반드시 다음 JSON 스키마로만 응답한다:\n"
        '{"fields": [{"field": "요청 필드명", "value": "추론값 또는 null", "confidence": 0.0, '
        '"evidence": "40자 내외 근거"}]}'
    )
    user_prompt = f"알려진 정보: {present_fields}\n요청 필드: {missing_fields}"

    with tool_span("infer_name_fields_with_llm", model="gpt-4.1-mini"):
        response = chat(
            "DEPLOYMENT_GPT41_MINI",
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    parsed = json.loads(response.choices[0].message.content)

    result = {f: {"value": None, "confidence": 0.0, "evidence": ""} for f in missing_fields}
    for entry in parsed.get("fields", []):
        f = entry.get("field")
        if f in result:
            result[f] = {
                "value": entry.get("value"),
                "confidence": max(0.0, min(1.0, float(entry.get("confidence") or 0.0))),
                "evidence": entry.get("evidence", ""),
            }
    return result


def find_row_completion(row: dict, meta_con) -> dict:
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

    eng_name = str(present.get("영문명", "")).strip()
    query_text = " ".join(str(v) for v in present.values())

    candidate_meta_row, match_confidence, match_evidence = None, 0.0, ""

    if eng_name:
        exact = exact_match_meta_db(meta_con, eng_name)
        if exact["found"]:
            candidate_meta_row = exact["meta_row"]
            match_confidence, match_evidence = 1.0, "영문명 정확 매칭"

    if candidate_meta_row is None:
        from llm_client import embed
        candidates = retrieve_candidates(
            meta_con, eng_name, query_text, embed, top_k=1,
            floor=SIMILARITY_PREFILTER_FLOOR, include_glossary_boost=True,
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
    llm_result = infer_name_fields_with_llm(present, missing)
    proposed = {f: v["value"] for f, v in llm_result.items() if v.get("value")}
    if not proposed:
        return None
    confidences = [v["confidence"] for f, v in llm_result.items() if v.get("value")]
    evidence = "; ".join(f"{f}: {v['evidence']}" for f, v in llm_result.items() if v.get("value"))
    return {"missing_fields": missing, "proposed_fill": proposed, "source": "llm_inference",
            "confidence": round(sum(confidences) / len(confidences), 4), "evidence": evidence}


# ── 담당자 확인 (행 보완 제안 - 파싱 완료 후 일괄) ───────────
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
    if raw == "approved":
        return {"decision": "approved", "approved_row_indices": [c["row_index"] for c in pending_completions]}
    return {"decision": "rejected", "approved_row_indices": []}


# ── 담당자 확인 (헤더 매핑 실패 시) ─────────────────────────
def _console_header_confirm(payload: dict) -> dict:
    """
    기본 확인 방식: 콘솔 input() (단독 실행/테스트용)
    반환: {"decision": "approved", "selected_column": "..."} 또는 {"decision": "rejected"}
    """
    print("\n" + "=" * 60)
    print(f"[담당자 확인 요청 - 헤더 매핑 실패] 표준 필드: {payload['missing_std_field']}")
    for a in payload["attempts"]:
        print(f"  - {a['method']}: {a['detail']}")
    print("  원본 헤더 목록:")
    for i, h in enumerate(payload["all_headers"]):
        mark = f" (이미 매핑됨 -> {h['mapped_to']})" if h.get("mapped_to") else ""
        print(f"    [{i}] {h['name']} (샘플: {h['sample']}){mark}")
    print("=" * 60)
    ans = input("매핑할 헤더의 인덱스를 입력하세요 (없으면 그냥 엔터): ").strip()
    if not ans:
        return {"decision": "rejected"}
    idx = int(ans)
    return {"decision": "approved", "selected_column": payload["all_headers"][idx]["name"]}


def _build_header_confirmation_payload(std_field: str, header_mapping: dict, df: pd.DataFrame,
                                        llm_results: list) -> dict:
    """헤더 매핑 실패 시 담당자에게 보여줄 payload 구성 (시도 내역 + 원본 헤더 전체 + LLM 제안)"""
    attempts = [{
        "method": "규칙 매칭",
        "detail": f"키워드({', '.join(STANDARD_FIELDS[std_field])}) 미포함",
    }]

    llm_attempt = next((m for m in llm_results if m.get("std_field") == std_field), None)
    if llm_attempt:
        if llm_attempt.get("matched_column"):
            attempts.append({
                "method": "LLM 판단 (gpt-4.1-mini)",
                "detail": (
                    f"후보 \"{llm_attempt['matched_column']}\" 제안, "
                    f"confidence {llm_attempt.get('confidence')} "
                    f"(기준 {HEADER_LLM_CONFIDENCE_FLOOR} 미달로 미채택)"
                ),
            })
        else:
            attempts.append({
                "method": "LLM 판단 (gpt-4.1-mini)",
                "detail": llm_attempt.get("evidence", "적합한 후보를 찾지 못함"),
            })

    all_headers = []
    for c in df.columns:
        mapped_to = next((f for f, col in header_mapping.items() if col == c), None)
        non_null = df[c].dropna()
        sample = str(non_null.iloc[0]) if not non_null.empty else ""
        all_headers.append({"name": str(c), "sample": sample, "mapped_to": mapped_to})

    suggested_column = llm_attempt.get("matched_column") if llm_attempt else None

    return {
        "type": "header_mapping_confirmation",
        "missing_std_field": std_field,
        "attempts": attempts,
        "all_headers": all_headers,
        "suggested_column": suggested_column,
    }


# ── 오케스트레이션 ───────────────────────────────────────────
@instrument_agent("Parsing Agent")
def run_parsing(file_path: str, confirm_fn=None) -> dict:
    """
    confirm_fn: payload(dict) -> {"decision": "approved", "selected_column": str} | {"decision": "rejected"}
    None이면 콘솔 input()으로 동작(단독 실행용). LangGraph 노드에서는
    interrupt()를 호출하는 confirm_fn을 주입해 실제 HITL로 동작시킴.
    """
    state = {
        "raw_file": file_path,
        "parsed_rows": [],
        "failed_rows": [],
        "total_count": 0,
        "parse_success_rate": 0.0,
        "candidate_tables": {},
        "header_mapping_source": {},       # {표준필드: "rule" | "llm" | "human"}
        "llm_mapping_confidence": {},      # {표준필드: float}
        "llm_mapping_evidence": {},        # {표준필드: str}
        "header_mapping_status": "resolved",  # "resolved" | "failed"
    }

    # Step 1: 파일 로드 + 헤더 탐지
    df = parse_excel_to_df(file_path, confirm_fn=confirm_fn)

    # Step 2: 규칙 기반 매핑
    header_mapping = map_columns_by_header(df)
    for f in header_mapping:
        state["header_mapping_source"][f] = "rule"

    missing_std_fields = [f for f in REQUIRED_FIELDS if f not in header_mapping]
    llm_results = []

    # Step 2.5: LLM fallback (규칙 매칭 실패한 필드만, 예외 케이스에서만 호출)
    if missing_std_fields:
        unmapped_columns = [str(c) for c in df.columns if c not in header_mapping.values()]
        llm_results = generate_header_mapping_judgment(missing_std_fields, df, unmapped_columns)

        for m in llm_results:
            std_field = m.get("std_field")
            matched_column = m.get("matched_column")
            confidence = m.get("confidence") or 0.0
            if (
                std_field in missing_std_fields
                and matched_column in unmapped_columns
                and confidence >= HEADER_LLM_CONFIDENCE_FLOOR
            ):
                header_mapping[std_field] = matched_column
                state["header_mapping_source"][std_field] = "llm"
                state["llm_mapping_confidence"][std_field] = confidence
                state["llm_mapping_evidence"][std_field] = m.get("evidence", "")

        missing_std_fields = [f for f in REQUIRED_FIELDS if f not in header_mapping]

    # Step 3: 규칙+LLM으로도 못 채운 필드가 남으면 담당자 확인으로 라우팅
    #         (기존처럼 ValueError로 파일 전체를 죽이지 않음 - "침묵 금지" 제약 준수)
    fn = confirm_fn or _console_header_confirm
    unresolved_fields = []
    for std_field in list(missing_std_fields):
        payload = _build_header_confirmation_payload(std_field, header_mapping, df, llm_results)
        decision = fn(payload)

        if decision.get("decision") == "approved" and decision.get("selected_column"):
            header_mapping[std_field] = decision["selected_column"]
            state["header_mapping_source"][std_field] = "human"
        else:
            unresolved_fields.append(std_field)

    if unresolved_fields:
        state["header_mapping_status"] = "failed"
        state["total_count"] = len(df)
        state["failed_rows"] = [{
            "row_index": None,
            "row_data": None,
            "missing_fields": unresolved_fields,
        }]
        return state

    # Step 4: 행 단위 구조화 + 스키마 검증
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
        # Step 5: 후보 테이블 매핑 (영문명이 있을 때만 - 없으면 조회할 근거가 없음)
        eng_name = row.get("영문명")
        if eng_name:
            candidates = map_candidate_tables(str(eng_name).strip())
            state["candidate_tables"][eng_name] = candidates
        state["parsed_rows"].append(row)

    meta_con = None
    pending_completions = []
    rows_by_idx = {}

    for idx, raw_row in df.iterrows():
        # 출력 순서 고정: 영문명/한글명/항목설명/type/시점(기간). 헤더에서 못 찾은 필드는 None으로 비움.
        row = {
            std_field: (_json_safe(raw_row[header_mapping[std_field]]) if std_field in header_mapping else None)
            for std_field in OUTPUT_FIELD_ORDER
        }
        rows_by_idx[idx] = row

        present_name_fields = [
            f for f in NAME_FIELDS
            if row.get(f) is not None and str(row.get(f)).strip() not in ("", "nan")
        ]
        # 영문명/한글명/항목설명 중 1개 이상만 있으면(전부는 아님) 메타DB/LLM으로 나머지 보완 시도
        if present_name_fields and len(present_name_fields) < len(NAME_FIELDS):
            if meta_con is None:
                meta_con = duckdb.connect(META_DB_PATH)
                try:
                    meta_con.execute("LOAD vss;")
                except duckdb.Error:
                    pass
            completion = find_row_completion(row, meta_con)
            if completion:
                pending_completions.append({"row_index": idx, **completion})
                continue

        _finalize_row(idx, row)

    if meta_con is not None:
        meta_con.close()

    # Step 4.5: 보완 제안이 있으면 파싱 완료 후 한 번에 일괄 확인받아 반영
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

    file_path = sys.argv[1] if len(sys.argv) > 1 else "./data/sample_spec.xlsx"
    result = run_parsing(file_path)

    print(f"[헤더 매핑 상태] {result['header_mapping_status']}")
    print(f"[헤더 매핑 소스] {result['header_mapping_source']}")
    if result["header_mapping_status"] == "failed":
        print(f"[미해결 표준 필드] {result['failed_rows'][0]['missing_fields']}")
    else:
        print(f"[총 행 수] {result['total_count']}")
        print(f"[파싱 성공] {len(result['parsed_rows'])}건 ({result['parse_success_rate']*100:.1f}%)")
        print(f"[파싱 실패] {len(result['failed_rows'])}건")
        for f in result["failed_rows"]:
            print(f"   - row {f['row_index']}: 누락 필드 {f['missing_fields']}")