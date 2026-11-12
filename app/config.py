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

# 현민님 AI 코드(team-10_AI) 위치. 한 서버로 합쳐서 함수를 직접 불러요
AI_ENGINE_PATH = Path(os.getenv("AI_ENGINE_PATH", str(ROOT.parent / "team-10_AI"))).resolve()

# 업로드한 PDF 저장 위치. AI 퀴즈 생성이 서버 안 파일 경로를 읽어서 AI_PDF_ROOT와 같게 맞춰요
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(ROOT / "storage"))).resolve()

# 데모 기능([다음 날로], 시드 전환) 허용 여부
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

# 익명 ID를 받는 헤더 (프론트 src/api/client.js와 같아야 해요)
ANON_HEADER = "X-Anonymous-Id"
