"""
frontend/api_client.py

프론트엔드의 모든 백엔드 호출은 이 모듈을 통해서만 이뤄진다(직접 requests 호출 금지).
"""

import os
import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")


class APIClient:
    def upload_spec(self, file_bytes: bytes, filename: str) -> dict:
        files = {"file": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        r = requests.post(f"{BACKEND_URL}/api/specs/upload", files=files, timeout=120)
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


api_client = APIClient()
