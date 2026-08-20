"""
backend/main.py

실행: uvicorn backend.main:app --reload --port 8000
Swagger UI: http://localhost:8000/docs
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import specs, pipeline

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


@app.get("/health")
def health():
    return {"status": "ok"}
