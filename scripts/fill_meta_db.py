"""
메타 DB column_spec 채우기 (3단계 중 규칙 기반 부분)

- 실데이터 DB(schemascout_data.duckdb)에서 테이블/컬럼/실제 data_type 조회
- 토큰 사전 기반으로 한글명/설명 자동 생성 (규칙 조합)
- 예외 컬럼(사전 조합으로 안 풀리는 것들)은 개별 매핑
- data_type은 실제 DB 타입을 기본으로 쓰되, 일부는 의도적으로 다르게 넣어
  type_mismatch 테스트 케이스로 사용 (TYPE_MISMATCH_INJECT 목록)
- LLM 다듬기는 여기서 하지 않음 (네트워크 제약) -> description_raw만 채우고,
  description(최종)은 일단 description_raw로 동일하게 넣어둔 뒤
  로컬에서 llm_polish_columns.py 실행 시 description만 갱신
"""

import os
import duckdb
from dotenv import load_dotenv

load_dotenv()

DATA_DB_PATH = os.environ.get("DATA_DB_PATH", "./db/schemascout_data.duckdb")
META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")

# ── 토큰 사전 ─────────────────────────────────────────────
TOKEN_DICT = {
    "loc": "로컬",
    "std": "장거리",
    "isd": "국제",
    "spl": "특수",
    "og": "발신",
    "ic": "수신",
    "t2t": "동일통신사",
    "t2m": "타사(모바일)",
    "t2f": "유선",
    "t2o": "기타사업자",
    "t2c": "콜센터",
    "mou": "통화시간",
    "rech": "충전",
    "amt": "금액",
    "vol": "사용량",
    "num": "건수",
    "total": "총",
    "max": "최대",
    "date": "일자",
    "last": "최근",
    "of": "",
    "day": "당일",
    "count": "횟수",
    "av": "평균",
    "data": "데이터",
    "others": "기타",
    "roam": "로밍",
    "monthly": "월정액",
    "sachet": "소액",
    "night": "야간",
    "pck": "패키지",
    "user": "이용여부",
    "fb": "페이스북",
    "onnet": "동일망",
    "offnet": "타사망",
}

# ── 예외 컬럼(규칙 조합으로 안 풀리는 것) ──────────────────
EXCEPTION_MAP = {
    "mobile_number": ("전화번호", "고객 식별용 이동전화번호"),
    "circle_id": ("서비스권역코드", "통신 서비스 제공 지역(circle) 코드"),
    "aon": ("가입기간(일)", "가입일로부터 해당 시점까지 경과 일수(Age On Network)"),
    "aon_at_first_seen": ("최초관측시가입기간", "고객이 데이터에 처음 등장한 시점의 가입기간(일)"),
    "first_seen_month": ("최초관측월", "고객이 데이터에 처음 등장한 월(YYYYMM)"),
    "churn_month": ("이탈월", "고객이 이탈(해지)한 월(YYYYMM), 활성 고객은 NULL"),
    "arpu": ("가입자당평균매출", "Average Revenue Per User, 해당 월 1인당 평균 매출"),
    "arpu_2g": ("2G가입자당평균매출", "2G 데이터 서비스 관련 가입자당 평균 매출"),
    "arpu_3g": ("3G가입자당평균매출", "3G 데이터 서비스 관련 가입자당 평균 매출"),
    "vbc_3g": ("3G볼륨기반과금액", "Volume Based Charging, 3G 사용량 기반 과금 금액"),
    "night_pck_user": ("야간패키지가입여부", "야간 전용 요금제 가입 여부(0/1)"),
    "fb_user": ("페이스북이용여부", "페이스북 무료/우대 데이터 서비스 이용 여부(0/1)"),
    "month": ("기준월", "데이터 기준 연월(YYYYMM)"),
}

# ── 규칙 기반 조합 ─────────────────────────────────────────
def rule_based_name_desc(col: str):
    if col in EXCEPTION_MAP:
        return EXCEPTION_MAP[col]
    tokens = col.split("_")
    kor_parts = []
    for tok in tokens:
        if tok.isdigit():
            continue
        mapped = TOKEN_DICT.get(tok, tok)
        if mapped:
            kor_parts.append(mapped)
    kor_name = " ".join(kor_parts) if kor_parts else col
    desc = f"{kor_name} 관련 값 (원본 컬럼명: {col})"
    return (kor_name, desc)


# ── data_type 의도적 불일치 주입 대상 ───────────────────────
# {테이블: {컬럼: 명세서에 잘못 기재할 타입}}
TYPE_MISMATCH_INJECT = {
    "raw_telecom_fact_call_outgoing": {"onnet_mou": "INTEGER"},          # 실제 DOUBLE -> 명세엔 INTEGER
    "raw_telecom_fact_recharge_voice": {"total_rech_amt": "VARCHAR"},    # 실제 DOUBLE -> 명세엔 VARCHAR
    "raw_telecom_fact_data_usage": {"vol_3g_mb": "INTEGER"},             # 실제 DOUBLE -> 명세엔 INTEGER
    "raw_telecom_fact_recharge_data": {"total_rech_data": "DOUBLE"},     # 실제 BIGINT -> 명세엔 DOUBLE
    "raw_telecom_dim_customer": {"churn_month": "VARCHAR"},              # 실제 DOUBLE -> 명세엔 VARCHAR
}


def main():
    data_con = duckdb.connect(DATA_DB_PATH, read_only=True)
    meta_con = duckdb.connect(META_DB_PATH)

    tables = data_con.execute(
        "SELECT table_name FROM information_schema.tables ORDER BY table_name"
    ).fetchall()

    rows_table_catalog = []
    rows_column_spec = []
    mismatch_log = []

    for (tname,) in tables:
        table_id = tname  # 단순화: table_id = table_name 그대로 사용
        row_count = data_con.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
        rows_table_catalog.append((table_id, tname, "telecom", row_count, f"{tname} 원본 데이터"))

        cols = data_con.execute(
            f"SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name='{tname}' ORDER BY ordinal_position"
        ).fetchall()

        for col_name, actual_type in cols:
            kor_name, desc = rule_based_name_desc(col_name)
            spec_type = actual_type
            if tname in TYPE_MISMATCH_INJECT and col_name in TYPE_MISMATCH_INJECT[tname]:
                spec_type = TYPE_MISMATCH_INJECT[tname][col_name]
                mismatch_log.append((tname, col_name, actual_type, spec_type))

            column_id = f"{table_id}.{col_name}"
            rows_column_spec.append((
                column_id, table_id, col_name, spec_type, kor_name, desc,
                None, None,  # tag, confidence: 파이프라인 실행 전이라 NULL
            ))

    # ── 메타 DB 적재 ─────────────────────────────────────
    meta_con.execute("DELETE FROM table_catalog")
    meta_con.execute("DELETE FROM column_spec")
    meta_con.executemany(
        "INSERT INTO table_catalog (table_id, table_name, source_system, row_count, description) VALUES (?,?,?,?,?)",
        rows_table_catalog,
    )
    meta_con.executemany(
        "INSERT INTO column_spec (column_id, table_id, column_name, data_type, description, tag) "
        "VALUES (?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], f"{r[4]} | {r[5]}", r[6]) for r in rows_column_spec],
    )

    # ── table_relationships 5건 등록 ────────────────────
    meta_con.execute("DELETE FROM table_relationships")
    fact_tables = [t for (t,) in tables if t != "raw_telecom_dim_customer"]
    rel_rows = []
    for ft in fact_tables:
        rel_rows.append((
            f"rel_dim_customer__{ft}",
            "raw_telecom_dim_customer", ft,
            "mobile_number", "1:N", "manual", None,
        ))
    meta_con.executemany(
        "INSERT INTO table_relationships "
        "(relationship_id, from_table_id, to_table_id, join_key, relation_type, source, confidence) "
        "VALUES (?,?,?,?,?,?,?)",
        rel_rows,
    )

    n_tables = meta_con.execute("SELECT COUNT(*) FROM table_catalog").fetchone()[0]
    n_cols = meta_con.execute("SELECT COUNT(*) FROM column_spec").fetchone()[0]
    n_rels = meta_con.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0]

    print(f"[table_catalog] {n_tables}개 테이블 등록")
    print(f"[column_spec]  {n_cols}개 컬럼 등록")
    print(f"[table_relationships] {n_rels}건 등록")
    print("\n[의도적으로 넣은 type_mismatch 테스트 케이스]")
    for tname, col, actual, spec in mismatch_log:
        print(f"  {tname}.{col} : 실제={actual} / 명세={spec}")

    data_con.close()
    meta_con.close()


if __name__ == "__main__":
    main()
