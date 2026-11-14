from uuid import UUID

from pydantic import BaseModel


class Answer(BaseModel):
    questionId: str
    choice: int | None = None
    text: str | None = None
    confidence: str | None = None


class SubmitIn(BaseModel):
    quizId: UUID
    answers: list[Answer]
    savedGrading: bool = False
