"""분배안 P2 (RC-02~05) — 프론트 EP.replan"""
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ApiError, ok
from app.db.session import get_conn
from app.services.planner import Planner
from app.services.plan_draft import PlanDraftService
from app.schemas.plan_draft import PlanApproval
from app.schemas.replan import ReplanApproval
from app.services.replan_options import ReplanOptionsService

router = APIRouter(prefix="/api/plan", tags=["계획"])


@router.get("/replan/options")
def replan_options(saved: int = Query(0, ge=0, le=1), user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    engine=conn.engine
    conn.close()
    return ok(ReplanOptionsService(engine).options(user['id'],saved=bool(saved)))


@router.post("/replan")
def replan_apply(body: ReplanApproval, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    engine=conn.engine
    conn.close()
    return ok(ReplanOptionsService(engine).apply(user['id'],str(body.previewId),body.key))


@router.get('/draft')
def initial_draft(draftSubjectIds: str | None = None, saved: int = Query(0, ge=0, le=1),
                  user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    identifiers = None
    if draftSubjectIds is not None:
        try:
            identifiers = tuple(str(UUID(value)) for value in draftSubjectIds.split(','))
            if len(identifiers) != len(set(identifiers)):
                raise ValueError
        except ValueError:
            raise ApiError('INVALID_INPUT', '중복 없는 draft 과목 UUID 목록이 필요해요', 422) from None
    engine = conn.engine
    conn.close()
    return ok(PlanDraftService(engine).draft(user['id'], draft_subject_ids=identifiers, saved=bool(saved)))


@router.post('/approve')
def initial_approve(body: PlanApproval, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    engine = conn.engine
    conn.close()
    return ok(PlanDraftService(engine).approve(user['id'], str(body.previewId)))
