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

UPLOAD_DIR = os.path.abspath(os.environ.get("UPLOAD_DIR", "./data/uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)


@router.post("/specs/upload")
async def upload_spec(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="xlsx 파일만 업로드할 수 있습니다.")

    # 보안: file.filename은 클라이언트가 완전히 제어하는 값이라 "../"나 절대경로가
    # 섞여 들어오면 os.path.join()이 UPLOAD_DIR 밖 경로로 튈 수 있다(경로 조작).
    # os.path.basename으로 디렉터리 구성요소를 전부 제거해 파일명만 남긴다.
    safe_filename = os.path.basename(file.filename)
    if not safe_filename or not safe_filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="유효하지 않은 파일명입니다.")

    upload_id = str(uuid.uuid4())
    save_path = os.path.abspath(os.path.join(UPLOAD_DIR, f"{upload_id}_{safe_filename}"))

    # 이중 방어: 정제 후에도 결과 경로가 UPLOAD_DIR 하위인지 최종 확인.
    if os.path.commonpath([save_path, UPLOAD_DIR]) != UPLOAD_DIR:
        raise HTTPException(status_code=400, detail="유효하지 않은 파일 경로입니다.")

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