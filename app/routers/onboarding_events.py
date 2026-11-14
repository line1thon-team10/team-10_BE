"""Initial S2 calendar CRUD. Natural-language events are handled by O-03."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.onboarding_events import CalendarQuery, CalendarResponse, EventCreate, EventPatch
from app.services.onboarding_events import OnboardingEventsService

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
