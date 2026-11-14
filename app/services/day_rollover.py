"""Settle missed local calendar dates once, before serving ordinary-user APIs."""
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.users import UserRepository
from app.services.focus import FocusService
from app.services.planner import Planner


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class DayRolloverService:
    def __init__(self, conn, *, clock=None):
        self.conn, self.clock = conn, clock or now_utc

    def reconcile(self, user):
        """The caller owns the short transaction; failures never skip a date."""
        at = self.clock()
        today = at.astimezone(ZoneInfo(user['timezone'])).date()
        if user['demo_date'] is not None or today == today.min:
            return user
        yesterday = today - timedelta(days=1)
        if user['last_assessed_date'] is not None and user['last_assessed_date'] >= yesterday:
            return user
        try:
            fresh = lock_user(self.conn, UUID(user['id']))
        except UserBusyError:
            raise ApiError('REQUEST_IN_PROGRESS', '날짜 마감을 처리 중이에요. 잠시 후 다시 시도해 주세요', 409) from None
        if fresh is None:
            raise ApiError('UNKNOWN_SESSION', '알 수 없는 익명 ID예요. 새로 발급받아 주세요', 401)
        user = fresh.to_mapping()
        # Another request may have completed the same dates before this lock.
        today = at.astimezone(ZoneInfo(user['timezone'])).date()
        yesterday = today - timedelta(days=1)
        if user['demo_date'] is not None or (user['last_assessed_date'] is not None and user['last_assessed_date'] >= yesterday):
            return user
        plan = self.conn.execute(select(t.plans).where(t.plans.c.user_id == user['id'])).mappings().first()
        if plan is None:
            self._mark(user['id'], yesterday, at)
            return self._user(user['id'])
        if user['last_assessed_date'] is None:
            zone = ZoneInfo(user['timezone'])
            candidates = [_aware(user['created_at']).astimezone(zone).date(),
                          _aware(plan['approved_at']).astimezone(zone).date()]
            first_task = self.conn.scalar(select(func.min(t.study_tasks.c.scheduled_date)).where(
                t.study_tasks.c.plan_id == plan['id']))
            if first_task is not None:
                candidates.append(first_task)
            day = min(candidates)
        else:
            day = user['last_assessed_date'] + timedelta(days=1)
        # Settle the real timeout, but preserve still-running/paused sessions and
        # their midnight-crossing segments, as required by focus.md.
        focus = FocusService(self.conn, user, today)
        focus.at = at
        focus._active_row()
        while day < today:
            current = self._user(user['id'])
            Planner(self.conn, current).close_day(day, allow_active=True, at=at)
            self._archive(user['id'], day + timedelta(days=1), at)
            self._mark(user['id'], day, at)
            day += timedelta(days=1)
        # A new plan may have no closed dates yet; remember that baseline too.
        if day >= today:
            self._mark(user['id'], yesterday, at)
        return self._user(user['id'])

    def _user(self, user_id):
        return UserRepository(self.conn).get(UUID(user_id)).to_mapping()

    def _mark(self, user_id, day, at):
        self.conn.execute(update(t.users).where(t.users.c.id == user_id).values(last_assessed_date=day, updated_at=at))

    def _archive(self, user_id, day, at):
        self.conn.execute(update(t.subjects).where(t.subjects.c.user_id == user_id,
            t.subjects.c.status == 'active', t.subjects.c.exam_date < day).values(
                status='archived', archive_reason='exam_finished', archived_at=at, updated_at=at))
