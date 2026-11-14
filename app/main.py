"""피뢰침 백엔드 서버 진입점.

실행:  .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
문서:  http://localhost:8000/docs
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai import engine as ai_engine
from app.config import CORS_ORIGINS, DATABASE_URL, DEMO_MODE
from app.core.cache import ApiNoStoreMiddleware
from app.core.responses import install_error_handlers, ok
from app.db.session import check_db_revision
from app.routers import empty_time, calendar, books, demo, materials, onboarding, onboarding_events, plan, quiz, quiz_answers, session, study, subjects, today, units


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_db_revision()
    yield


app = FastAPI(title="피뢰침 백엔드", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ApiNoStoreMiddleware)
install_error_handlers(app)

app.include_router(empty_time.router)
app.include_router(calendar.router)
app.include_router(subjects.router)
app.include_router(session.router)
app.include_router(demo.router)
app.include_router(onboarding.router)
app.include_router(onboarding_events.router)
app.include_router(today.router)
app.include_router(books.router)
app.include_router(quiz.router)
app.include_router(quiz_answers.router)
app.include_router(plan.router)
app.include_router(study.router)
app.include_router(materials.router)
app.include_router(units.router)


@app.get("/api/health", tags=["상태"])
def health(ai: bool = False):
    """서버 상태. ?ai=true면 AI 모듈을 실제로 불러와서 확인해요 (느릴 수 있어요)."""
    data = {"status": "ok", "db": DATABASE_URL.split(":")[0], "demoMode": DEMO_MODE}
    if ai:
        data["ai"] = ai_engine.status()
    return ok(data)
