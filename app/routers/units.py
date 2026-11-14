"""Owned material unit structure."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.units import UnitsQuery, UnitsResponse
from app.services.units import UnitService

router = APIRouter(prefix="/api/units", tags=["자료 단원"])


@router.get("", response_model=UnitsResponse)
def get_units(query: Annotated[UnitsQuery, Query()], user: dict = Depends(current_user),
              conn: Connection = Depends(get_conn)):
    return ok(UnitService(conn).get(user["id"], query.subjectId, query.materialId))
