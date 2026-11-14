"""Rebuild delegates to the same preserved-fact candidate and approval path."""
from sqlalchemy import select
from app.core.responses import ApiError
from app.db import schema as t
from app.services.plan_resplit import PlanResplitService


class PlanRebuildService:
    def __init__(self,engine):
        self.engine=engine

    def rebuild(self,user_id,body):
        with self.engine.connect() as conn:
            if conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id==user_id)) is None:
                raise ApiError('NO_PLAN','승인된 계획이 필요해요',400)
        return PlanResplitService(self.engine).prepare(user_id,body.request or '남은 전체 범위를 다시 배분',
            saved=body.saved,deterministic=body.request is None)
