"""Intervals reserve their real overlap even if their scheduled date is earlier."""
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select
from app.db import schema as t
from tests.test_mastery_rules import mastery_case, submit_quiz, AT
from tests.test_remedial_tasks import remedial_case
from tests.test_onboarding_events import at


def test_two_day_interval_keeps_next_study_morning_unavailable(api_client, domain_connection, remedial_case):
    conn, c = domain_connection, remedial_case
    sid, block, task = (str(uuid4()) for _ in range(3))
    conn.execute(t.subjects.insert().values(id=sid, user_id=c['user'].id, code='CROSS_DAY', name='다른 과목', color='blue', status='active', created_at=AT, updated_at=AT))
    conn.execute(t.study_blocks.insert().values(id=block, subject_id=sid, task_type='study', created_at=AT))
    conn.execute(t.study_tasks.insert().values(id=task, plan_id=c['plan'], subject_id=sid, block_id=block,
        task_type='study', title='장기 일정과 겹친 배치', scheduled_date=c['nextday']-timedelta(days=2),
        starts_at=at(c['nextday']-timedelta(days=2), '22:00'), ends_at=at(c['nextday'], '10:15'),
        estimated_minutes=60, status='planned', created_at=AT))
    before = dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == task)).mappings().one())
    conn.commit()
    submit_quiz(api_client, conn, c, c['questions'][0], 14)
    rows = conn.execute(select(t.study_tasks).where(t.study_tasks.c.task_type == 'remedial')).mappings().all()
    assert rows
    for row in rows:
        assert row['scheduled_date'] == c['nextday']
        assert row['starts_at'] >= at(c['nextday'], '10:15').replace(tzinfo=row['starts_at'].tzinfo)
        assert row['ends_at'] <= at(c['nextday'], '23:00').replace(tzinfo=row['ends_at'].tzinfo)
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == task)).mappings().one()) == before
