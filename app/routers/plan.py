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

router = APIRouter(prefix="/api/plan", tags=["계획"])


@router.get("/replan/options")
def replan_options(saved: int = 0, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """밀렸을 때 안 3개 (A 시간 늘리기 / B 범위 줄이기 / C 압축하기). 계산은 규칙, 설명 문구도 지금은 규칙"""
    view = Planner(conn, user).replan_view(saved=bool(saved))
    if view is None:
        raise ApiError("NO_REPLAN", "지금은 밀린 분량이 없어요", 404)
    return ok(view)


@router.post("/replan")
def replan_apply(key: str = Body(..., embed=True), user: dict = Depends(current_user),
                 conn: Connection = Depends(get_conn)):
    """고른 안만 적용 (RC-04). 보충 학습은 유지 (RC-05) → 회복 박스 이벤트"""
    return ok({"event": Planner(conn, user).apply(key)})


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
