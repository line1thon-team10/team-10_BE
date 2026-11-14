"""S1 persisted onboarding state. Natural-language writes belong to O-03."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.onboarding import OnboardingQuery, OnboardingResponse
from app.services.onboarding import OnboardingService

router = APIRouter(prefix="/api/onboarding", tags=["온보딩"])


@router.get("", response_model=OnboardingResponse)
def get_onboarding(query: Annotated[OnboardingQuery, Query()], user: dict = Depends(current_user),
                   conn: Connection = Depends(get_conn)):
    return ok(OnboardingService(conn, user).get(query.subjectId))
