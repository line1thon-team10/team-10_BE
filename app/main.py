"""피뢰침 백엔드 서버 진입점.

실행:  .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
문서:  http://localhost:8000/docs
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai import engine as ai_engine
from app.config import CORS_ORIGINS, DATABASE_URL, DEMO_MODE
from app.core.responses import install_error_handlers, ok
from app.db.session import init_db
from app.routers import demo, session


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="피뢰침 백엔드", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_error_handlers(app)

app.include_router(session.router)
app.include_router(demo.router)


@app.get("/api/health", tags=["상태"])
def health(ai: bool = False):
    """서버 상태. ?ai=true면 AI 모듈을 실제로 불러와서 확인해요 (느릴 수 있어요)."""
    data = {"status": "ok", "db": DATABASE_URL.split(":")[0], "demoMode": DEMO_MODE}
    if ai:
        data["ai"] = ai_engine.status()
    return ok(data)
