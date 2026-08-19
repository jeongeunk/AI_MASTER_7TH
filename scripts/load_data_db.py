"""
실데이터 DB(schemascout_data.duckdb) 생성 + 6개 xlsx 파일 적재

네이밍 컨벤션: raw_<소스시스템>_<테이블명>  (소스시스템: telecom)
"""

import duckdb
import pandas as pd
import os
from dotenv import load_dotenv

load_dotenv()

XLSX_DIR = os.environ.get("DATA_XLSX_DIR", "./data")
DB_PATH = os.environ.get("DATA_DB_PATH", "./db/schemascout_data.duckdb")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

TABLES = {
    "dim_customer": "raw_telecom_dim_customer",
    "fact_call_outgoing": "raw_telecom_fact_call_outgoing",
    "fact_call_incoming": "raw_telecom_fact_call_incoming",
    "fact_recharge_voice": "raw_telecom_fact_recharge_voice",
    "fact_recharge_data": "raw_telecom_fact_recharge_data",
    "fact_data_usage": "raw_telecom_fact_data_usage",
}

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

con = duckdb.connect(DB_PATH)

for src_file, table_name in TABLES.items():
    xlsx_path = f"{XLSX_DIR}/{src_file}.xlsx"
    df = pd.read_excel(xlsx_path)
    con.register("tmp_df", df)
    con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM tmp_df")
    con.unregister("tmp_df")
    cnt = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    print(f"[LOADED] {table_name}: {cnt:,} rows")

print("\n[카탈로그 확인]")
print(con.execute("SHOW TABLES").fetchdf())

con.close()
print(f"\n[DONE] {DB_PATH} 생성 완료")
