"""
backend/api/pipeline.py

- POST /api/pipeline/start                    : 파이프라인 백그라운드 실행 시작
- GET  /api/pipeline/{thread_id}/events        : 노드별 진행 로그 폴링 (tool/LLM 정보 포함)
- POST /api/pipeline/{thread_id}/confirm       : 담당자 확인(승인/거절) 응답 제출
- GET  /api/pipeline/{thread_id}/results       : 컬럼별 결과 테이블 조회
- GET  /api/pipeline/{thread_id}/download      : 최종 명세서 엑셀 다운로드
"""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.core.pipeline_runner import (
    start_pipeline,
    get_events_since,
    submit_confirmation,
    get_results,
    get_download_path,
)

router = APIRouter()


class StartRequest(BaseModel):
    file_path: str


class ConfirmRequest(BaseModel):
    decision: str  # "approved" | "rejected"


@router.post("/pipeline/start")
def start(req: StartRequest):
    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=404, detail="업로드된 파일을 찾을 수 없습니다.")
    thread_id = start_pipeline(req.file_path)
    return {"thread_id": thread_id}


@router.get("/pipeline/{thread_id}/events")
def events(thread_id: str, since: int = 0):
    try:
        return get_events_since(thread_id, since)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/pipeline/{thread_id}/confirm")
def confirm(thread_id: str, req: ConfirmRequest):
    if req.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision은 approved 또는 rejected여야 합니다.")
    try:
        submit_confirmation(thread_id, req.decision)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@router.get("/pipeline/{thread_id}/results")
def results(thread_id: str):
    try:
        return get_results(thread_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/pipeline/{thread_id}/download")
def download(thread_id: str):
    try:
        path = get_download_path(thread_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return FileResponse(path, filename=os.path.basename(path),
                         media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
