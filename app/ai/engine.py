"""현민님 AI 코드(team-10_AI)를 같은 서버에서 직접 불러요.

AI 코드는 `services.xxx` 모듈 이름을 쓰기 때문에, AI 저장소 폴더를 파이썬 경로에 추가해요.
처음 쓸 때만 불러오고(lazy), 못 불러오면 status()로 이유를 알려줘요.
"""
import importlib
import os
import sys
from functools import lru_cache

from app.config import AI_ENGINE_PATH, STORAGE_DIR

MODULES = {
    "pdf_analyzer": "services.pdf_analyzer",
    "analysis_contract": "services.analysis_contract",
    "quiz_bank": "services.quiz_bank",
    "quiz_grader": "services.quiz_grader",
    "essay_grader": "services.essay_grader",
    "learning_metrics": "services.learning_metrics",
    "backend_adapter": "services.backend_adapter",
    "ai_tutor": "services.ai_tutor",
    "study_planner": "services.study_planner",
    "study_replanner": "services.study_replanner",
}


def _prepare_path() -> None:
    if not (AI_ENGINE_PATH / "services").is_dir():
        raise ModuleNotFoundError(
            "AI 소스를 찾지 못했어요. BE 루트에서 "
            "git submodule update --init --recursive를 실행하거나 AI_ENGINE_PATH를 확인하세요."
        )
    path = str(AI_ENGINE_PATH)
    if path not in sys.path:
        sys.path.insert(0, path)
    # AI 퀴즈 생성은 AI_PDF_ROOT 안의 PDF만 읽어요 → 우리 업로드 폴더로 맞춰요
    os.environ.setdefault("AI_PDF_ROOT", str(STORAGE_DIR))


@lru_cache(maxsize=None)
def module(name: str):
    _prepare_path()
    return importlib.import_module(MODULES[name])


def status() -> dict:
    """어떤 AI 모듈을 불러올 수 있는지 (/api/health에서 확인용)"""
    result = {"path": str(AI_ENGINE_PATH), "found": (AI_ENGINE_PATH / "services").is_dir(),
              "groqKey": bool(os.getenv("GROQ_API_KEY")), "modules": {}}
    if not result["found"]:
        return result
    for name in MODULES:
        try:
            module(name)
            result["modules"][name] = "ok"
        except Exception as e:  # noqa: BLE001 — 상태 확인용이라 모든 오류를 보여줘요
            result["modules"][name] = f"{type(e).__name__}: {e}"[:200]
    return result
