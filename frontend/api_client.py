"""
frontend/api_client.py

프론트엔드의 모든 백엔드 호출은 이 모듈을 통해서만 이뤄진다(직접 requests 호출 금지).
"""

import os
import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")


class SpecParsingFailed(Exception):
    """미리보기(동기) 파싱이 실패했을 때 - 원인(message)과, 파일이 이미 저장돼 있다면
    그 경로(file_path)를 같이 담는다. file_path가 있으면 화면에서 "미리보기 없이
    파이프라인 시작"으로 바로 넘어갈 수 있다(담당자 확인이 필요한 파일이 그 예)."""
    def __init__(self, message: str, file_path: str = None, hint: str = None):
        super().__init__(message)
        self.message = message
        self.file_path = file_path
        self.hint = hint


class APIClient:
    def upload_spec(self, file_bytes: bytes, filename: str) -> dict:
        files = {"file": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        r = requests.post(f"{BACKEND_URL}/api/specs/upload", files=files, timeout=120)
        if r.status_code == 422:
            # raise_for_status()는 detail 본문을 버리고 "422 Client Error: ..."라는
            # 껍데기 메시지만 남기므로, 여기서 먼저 detail을 꺼내 실제 사유를 보존한다.
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            if isinstance(detail, dict):
                raise SpecParsingFailed(
                    detail.get("message", "파싱 실패"),
                    file_path=detail.get("file_path"),
                    hint=detail.get("hint"),
                )
            raise SpecParsingFailed(str(detail) if detail else "파싱 실패 (사유 없음)")
        r.raise_for_status()
        return r.json()

    def start_pipeline(self, file_path: str) -> dict:
        r = requests.post(f"{BACKEND_URL}/api/pipeline/start", json={"file_path": file_path}, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_events(self, thread_id: str, since: int = 0) -> dict:
        r = requests.get(f"{BACKEND_URL}/api/pipeline/{thread_id}/events", params={"since": since}, timeout=30)
        r.raise_for_status()
        return r.json()

    def confirm(self, thread_id: str, decision: str) -> dict:
        r = requests.post(f"{BACKEND_URL}/api/pipeline/{thread_id}/confirm", json={"decision": decision}, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_results(self, thread_id: str) -> dict:
        r = requests.get(f"{BACKEND_URL}/api/pipeline/{thread_id}/results", timeout=30)
        r.raise_for_status()
        return r.json()

    def download_report(self, thread_id: str) -> bytes:
        r = requests.get(f"{BACKEND_URL}/api/pipeline/{thread_id}/download", timeout=60)
        r.raise_for_status()
        return r.content

    def get_kpi_report(self, eng_name: str = None) -> dict:
        params = {"eng_name": eng_name} if eng_name else {}
        r = requests.get(f"{BACKEND_URL}/api/kpi/report", params=params, timeout=30)
        r.raise_for_status()
        return r.json()


api_client = APIClient()
