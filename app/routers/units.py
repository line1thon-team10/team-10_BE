"""Owned material unit structure."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.units import UnitsQuery, UnitsResponse, UnitsEdit, UnitsApproval
from app.services.units import UnitService

router = APIRouter(prefix="/api/units", tags=["자료 단원"])


@router.get("", response_model=UnitsResponse)
def get_units(query: Annotated[UnitsQuery, Query()], user: dict = Depends(current_user),
              conn: Connection = Depends(get_conn)):
    return ok(UnitService(conn).get(user["id"], query.subjectId, query.materialId))


@router.put("")
def replace_units(body: UnitsEdit | UnitsApproval, user: dict = Depends(current_user),
                  conn: Connection = Depends(get_conn)):
    from app.services.material_structure import MaterialStructureService
    engine = conn.engine
    conn.close()
    service = MaterialStructureService(engine)
    if isinstance(body, UnitsApproval):
        return ok(service.approve(user["id"], body.previewId))
    return ok(service.edit(user["id"], body))
