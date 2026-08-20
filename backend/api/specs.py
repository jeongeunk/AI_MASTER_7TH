"""
backend/api/specs.py

POST /api/specs/upload — 명세서 엑셀 업로드 → Parsing Agent 실행 → 미리보기 반환
"""

import os
import sys
import shutil
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, UploadFile, File, HTTPException

from agents.parsing_agent import run_parsing

router = APIRouter()

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "./data/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@router.post("/specs/upload")
async def upload_spec(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="xlsx 파일만 업로드할 수 있습니다.")

    upload_id = str(uuid.uuid4())
    save_path = os.path.join(UPLOAD_DIR, f"{upload_id}_{file.filename}")
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        result = run_parsing(save_path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"파싱 실패: {e}")

    return {
        "upload_id": upload_id,
        "file_path": save_path,
        "total_count": result["total_count"],
        "success_count": len(result["parsed_rows"]),
        "fail_count": len(result["failed_rows"]),
        "failed_rows": result["failed_rows"][:20],  # 실패 사유 확인용 (최대 20건만)
        "preview": result["parsed_rows"][:10],  # 미리보기는 10건만
    }
