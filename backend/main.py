"""
backend/main.py

실행: uvicorn backend.main:app --reload --port 8000
Swagger UI: http://localhost:8000/docs
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import specs, pipeline, kpi

load_dotenv()


def _print_langsmith_startup_banner():
    """서버 시작 시 1회, LangSmith 트레이싱 켜짐/꺼짐 여부를 눈에 띄게 알린다.

    개발 중 모니터링용으로만 켜둔 걸 잊고 그대로 운영에 넘어가는 실수를 막기 위한
    최소한의 안전장치 - 강제는 아니고 "보이게" 만드는 것까지만이다. 평소 tool_span
    로그(📍/🤖/📝/💬/❌)와 겹치지 않는 이모지·테두리·영문 대문자 라벨로 구분되게
    해서, 로그가 쭉 흘러가는 중에도 눈에 걸리도록 설계했다.
    """
    tracing_on = os.environ.get("LANGSMITH_TRACING", "").strip().lower() == "true"
    border = "=" * 70
    print(border)
    if tracing_on:
        project = os.environ.get("LANGSMITH_PROJECT") or "(미지정)"
        print("🛰️  LANGSMITH TRACING: ON  (development monitoring only)")
        print("    - 이 서버의 모든 실행 트레이스가 LangSmith 클라우드로 전송됩니다.")
        print(f"    - 프로젝트: {project} / 무료 티어")
        print("    - 실 업무 데이터로는 이 설정을 사용하지 마세요 (개발·테스트 전용)")
        print(border)
        print("    운영 배포 시: .env의 LANGSMITH_TRACING을 false로 바꾸거나 그 줄을 삭제하세요.")
    else:
        print("🔒 LANGSMITH TRACING: OFF — 로컬 로그(콘솔 + run_metrics)만 사용")
        print(border)


_print_langsmith_startup_banner()

app = FastAPI(title="SchemaScout API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501"],  # Streamlit 기본 포트
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(specs.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(kpi.router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
