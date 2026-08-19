"""
[로컬 실행 전용] column_spec 설명을 LLM으로 자연스럽게 다듬기

fill_meta_db.py로 채운 column_spec.description은
"한글명 | 규칙조합 설명(원본 컬럼명: xxx)" 형태의 기계적인 문장입니다.
이 스크립트는 gpt-4.1-mini를 배치 호출해 담당자가 읽기 편한 한 문장으로 다듬어
description 컬럼을 덮어씁니다.

실행 전 확인:
- .env에 LLM_BASE_URL / LLM_API_KEY / DEPLOYMENT_GPT41_MINI가 설정되어 있어야 함
- schemascout_meta.duckdb가 로컬 db/ 폴더에 있어야 함
- pip install duckdb python-dotenv openai

실행: python llm_polish_columns.py
"""

import os
import sys
import json
from pathlib import Path

# llm_client.py가 프로젝트 루트에 있으므로, scripts/ 안에서 실행해도 찾을 수 있게 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
from llm_client import chat

META_DB_PATH = os.environ.get("META_DB_PATH", "./db/schemascout_meta.duckdb")
BATCH_SIZE = 20  # 한 번의 LLM 호출에 묶어서 보낼 컬럼 수 (비용/속도 절충)

SYSTEM_PROMPT = """너는 통신사 데이터 명세서를 다듬는 어시스턴트야.
입력으로 (컬럼ID, 한글명, 규칙기반 설명) 목록을 JSON 배열로 받는다.
각 항목의 설명을 담당자가 읽기 편한 자연스러운 한 문장(40자 내외)으로 다시 써라.
원래 의미를 벗어나거나 새로운 정보를 추가하지 마라.
출력은 반드시 다음 형식의 JSON 배열만: [{"column_id": "...", "description": "..."}]
다른 텍스트(설명, 코드블록 표시 등)는 절대 포함하지 마라.
"""


def polish_batch(rows: list[tuple]) -> dict:
    """rows: [(column_id, kor_name, raw_description), ...] -> {column_id: polished_description}"""
    payload = [
        {"column_id": r[0], "kor_name": r[1], "raw_description": r[2]}
        for r in rows
    ]
    resp = chat(
        "DEPLOYMENT_GPT41_MINI",
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.3,
    )
    text = resp.choices[0].message.content.strip()
    # 혹시 코드블록으로 감싸져 오면 제거
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    result = json.loads(text)
    return {item["column_id"]: item["description"] for item in result}


def main():
    con = duckdb.connect(META_DB_PATH)
    rows = con.execute(
        "SELECT column_id, description FROM column_spec ORDER BY column_id"
    ).fetchall()

    # description 컬럼에 "한글명 | 규칙설명" 형태로 들어있으므로 분리
    parsed = []
    for column_id, desc in rows:
        if " | " in desc:
            kor_name, raw_desc = desc.split(" | ", 1)
        else:
            kor_name, raw_desc = "", desc
        parsed.append((column_id, kor_name, raw_desc))

    updated = {}
    for i in range(0, len(parsed), BATCH_SIZE):
        batch = parsed[i : i + BATCH_SIZE]
        print(f"[polish] {i+1}~{i+len(batch)} / {len(parsed)} 처리 중...")
        try:
            polished = polish_batch(batch)
            updated.update(polished)
        except Exception as e:
            print(f"  [WARN] 배치 실패, 원본 유지: {e}")

    for column_id, new_desc in updated.items():
        con.execute(
            "UPDATE column_spec SET description = ?, updated_at = current_timestamp WHERE column_id = ?",
            [new_desc, column_id],
        )

    print(f"\n[DONE] {len(updated)}개 컬럼 설명 다듬기 완료 (전체 {len(parsed)}개 중)")
    con.close()


if __name__ == "__main__":
    main()
