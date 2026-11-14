from typing import Annotated
from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.calendar import CalendarRange
from app.services.calendar import CalendarService

router = APIRouter(prefix='/api', tags=['캘린더'])


@router.get('/calendar')
def calendar(query: Annotated[CalendarRange, Query()], user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(CalendarService(conn, user).view(query.from_date, query.to))
