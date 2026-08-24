"""
backend/api/pipeline.py

- POST /api/pipeline/start                    : 파이프라인 백그라운드 실행 시작
- GET  /api/pipeline/{thread_id}/events        : 노드별 진행 로그 폴링 (tool/LLM 정보 포함)
- POST /api/pipeline/{thread_id}/confirm       : 담당자 확인 응답 제출
      - 단순 승인/거절: {"decision": "approved"} 또는 {"decision": "rejected"}
      - 헤더 매핑 확인처럼 부가 정보가 필요한 경우:
        {"decision": {"decision": "approved", "selected_column": "컬럼명"}}
        또는 {"decision": {"decision": "rejected"}}
- GET  /api/pipeline/{thread_id}/results       : 컬럼별 결과 테이블 조회
- GET  /api/pipeline/{thread_id}/download      : 최종 명세서 엑셀 다운로드
"""

import os
from typing import Union

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
    # 기존(Meta Search/DB Validation): 단순 문자열 "approved" | "rejected"
    # 신규(헤더 매핑 확인 등 부가 정보가 필요한 케이스): {"decision": "approved"|"rejected", ...추가필드}
    decision: Union[str, dict]


def _validate_decision(decision: Union[str, dict]) -> None:
    if isinstance(decision, str):
        if decision not in ("approved", "rejected"):
            raise HTTPException(status_code=400, detail="decision 문자열은 approved 또는 rejected여야 합니다.")
        return
    if isinstance(decision, dict):
        inner = decision.get("decision")
        if inner not in ("approved", "rejected"):
            raise HTTPException(status_code=400, detail="decision.decision 필드는 approved 또는 rejected여야 합니다.")
        return
    raise HTTPException(status_code=400, detail="decision은 문자열 또는 객체여야 합니다.")


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
    _validate_decision(req.decision)
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