"""환경변수 설정. 값은 .env 또는 배포 환경변수에서 읽어요 (.env.example 참고)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


# 로컬은 SQLite 파일, 배포는 postgresql+psycopg://... 로 바꿔요
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'pirochim.db'}")

# 프론트 주소 (Vite 개발 서버 + Vercel 배포 주소)
CORS_ORIGINS = _list("CORS_ORIGINS", "http://localhost:5173")

# 기본은 BE가 커밋으로 고정한 submodule. 개발용 상대 경로도 BE 루트 기준이에요.
_ai_path = Path(os.getenv("AI_ENGINE_PATH", "").strip() or "vendor/team-10_AI")
AI_ENGINE_PATH = (_ai_path if _ai_path.is_absolute() else ROOT / _ai_path).resolve()

# 업로드한 PDF 저장 위치. AI 퀴즈 생성이 서버 안 파일 경로를 읽어서 AI_PDF_ROOT와 같게 맞춰요
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(ROOT / "storage"))).resolve()

# 데모 기능([다음 날로], 시드 전환) 허용 여부
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

# 익명 ID를 받는 헤더 (프론트 src/api/client.js와 같아야 해요)
ANON_HEADER = "X-Anonymous-Id"
