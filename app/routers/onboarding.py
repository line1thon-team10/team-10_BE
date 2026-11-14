"""S1 persisted onboarding state. Natural-language writes belong to O-03."""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ApiError, ok
from app.db.session import get_conn
from app.schemas.onboarding import OnboardingQuery, OnboardingResponse
from app.services.onboarding import OnboardingService
from app.services.onboarding_dialogue import OnboardingDialogueService
from app.schemas.onboarding_dialogue import DialogueCommand

router = APIRouter(prefix="/api/onboarding", tags=["온보딩"])


@router.get("", response_model=OnboardingResponse)
def get_onboarding(query: Annotated[OnboardingQuery, Query()], user: dict = Depends(current_user),
                   conn: Connection = Depends(get_conn)):
    return ok(OnboardingService(conn, user).get(query.subjectId))


@router.post('/message', response_model=OnboardingResponse)
def post_message(command: DialogueCommand, user: dict = Depends(current_user), conn: Connection = Depends(get_conn),
                 keys: list[str] | None = Header(None, alias='Idempotency-Key')):
    if keys is not None and len(keys) != 1:
        raise ApiError('INVALID_INPUT', '요청 키는 하나만 보내 주세요', 422)
    engine = conn.engine
    conn.close()
    return ok(OnboardingDialogueService(engine).run(user['id'], command.model_dump(),
              idempotency_key=keys[0] if keys else None))
