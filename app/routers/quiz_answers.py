from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.quiz_answers import SaveAnswerIn
from app.services.quiz_answers import save_answer

router = APIRouter(prefix="/api/quiz", tags=["퀴즈"])


@router.put("/{quizId}/answers/{questionId}")
def quiz_answer(quizId: UUID, questionId: UUID, body: SaveAnswerIn,
                user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """문항 답안과 채점 작업을 함께 저장하고 입력 revision만 돌려줘요."""
    return ok(save_answer(conn, user, str(quizId), str(questionId), body))
