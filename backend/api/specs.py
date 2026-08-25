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
        # 미리보기는 담당자 확인(interrupt) 없이 동기로 응답해야 하므로,
        # 규칙+LLM 매핑에도 실패한 필드는 바로 거절 처리해 422로 보고한다.
        # (실제 담당자 확인 UI는 /api/pipeline/start 이후 모니터링 화면에서 처리)
        result = run_parsing(save_path, confirm_fn=lambda payload: {"decision": "rejected"})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"파싱 실패: {e}")

    return {
        "upload_id": upload_id,
        "file_path": save_path,
        "total_count": result["total_count"],
        "success_count": len(result["parsed_rows"]),
        "fail_count": len(result["failed_rows"]),
        "header_mapping_status": result["header_mapping_status"],
        "unresolved_header_fields": result["unresolved_header_fields"],
        "rows": result["all_rows"],          # 원본 행 순서 그대로 전체 (성공/확인필요 모두 포함) - 업로드 화면 통합 표용
        "failed_rows": result["failed_rows"][:20],  # 하위 호환 (실패 사유만 따로 보고 싶을 때)
        "preview": result["parsed_rows"],           # 하위 호환 (성공분만 보고 싶을 때)
        "trace": result.get("trace_log"),    # 모니터링 화면 표시용: 시작/종료/소요시간 + tool 호출 내역
    }