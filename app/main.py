"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api.routes import router

app = FastAPI(title="AI 智能性能测试平台", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """返回单页前端。"""
    from pathlib import Path

    html_path = Path(__file__).parent / "static" / "index.html"
    return html_path.read_text(encoding="utf-8")
