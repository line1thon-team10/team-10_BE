"""Initial S2 calendar CRUD. Natural-language events are handled by O-03."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ApiError, ok
from app.db.session import get_conn
from app.schemas.onboarding_events import CalendarQuery, CalendarResponse, EventCreate, EventPatch
from app.services.onboarding_events import OnboardingEventsService
from app.services.onboarding_dialogue import OnboardingDialogueService
from app.schemas.onboarding_dialogue import VoiceCommand

router = APIRouter(prefix="/api/onboarding", tags=["온보딩 일정"])


@router.get("/calendar", response_model=CalendarResponse)
def get_calendar(query: Annotated[CalendarQuery, Query()], user: dict = Depends(current_user),
                 conn: Connection = Depends(get_conn)):
    return ok(OnboardingEventsService(conn, user).calendar())


@router.post("/events", response_model=CalendarResponse)
def create_event(command: EventCreate, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(OnboardingEventsService(conn, user).create(command))


@router.patch("/events/{event_id}", response_model=CalendarResponse)
def patch_event(event_id: UUID, command: EventPatch, user: dict = Depends(current_user),
                conn: Connection = Depends(get_conn)):
    return ok(OnboardingEventsService(conn, user).patch(event_id, command))


@router.delete("/events/{event_id}", response_model=CalendarResponse)
def delete_event(event_id: UUID, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(OnboardingEventsService(conn, user).delete(event_id))


@router.post('/events/voice', response_model=CalendarResponse)
def create_voice_event(command: VoiceCommand, user: dict = Depends(current_user), conn: Connection = Depends(get_conn),
                       keys: list[str] | None = Header(None, alias='Idempotency-Key')):
    if keys is not None and len(keys) != 1:
        raise ApiError('INVALID_INPUT', '요청 키는 하나만 보내 주세요', 422)
    engine = conn.engine
    conn.close()
    return ok(OnboardingDialogueService(engine).run(user['id'], command.model_dump(), mode='voice',
              idempotency_key=keys[0] if keys else None))
