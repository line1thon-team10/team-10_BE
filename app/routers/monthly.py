from typing import Annotated
from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.monthly import MonthQuery
from app.services.monthly import MonthlyService

router = APIRouter(prefix='/api/monthly', tags=['월간 집계'])


@router.get('')
def monthly(query: Annotated[MonthQuery, Query()], user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(MonthlyService(conn,user).view(query.month))
