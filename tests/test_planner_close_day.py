"""Normal-user closure preserves midnight focus and never mutates demo_date."""
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.db import schema as t
from app.repositories.users import UserRepository
from app.services.planner import Planner


def test_explicit_close_day_preserves_active_task_and_account_clock(api_client, settled_demo_user, domain_connection):
    conn,user=domain_connection,settled_demo_user
    source=conn.execute(select(t.study_tasks).join(t.subjects).where(t.subjects.c.user_id==user,
        t.study_tasks.c.scheduled_date==date(2026,11,9), t.study_tasks.c.status.in_(['planned','partial']))).mappings().first()
    conn.execute(update(t.users).where(t.users.c.id==user).values(demo_date=None))
    at=datetime(2026,11,10,0,0,tzinfo=timezone.utc)
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=user,task_id=source['id'],status='running',
        started_at=at,study_date=date(2026,11,9),created_at=at))
    fresh=UserRepository(conn).get(UUID(user)).to_mapping()
    Planner(conn,fresh).close_day(date(2026,11,9),allow_active=True,at=at)
    after=conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==source['id'])).mappings().one()
    assert dict(after)==dict(source)
    assert conn.scalar(select(t.study_tasks.c.id).where(t.study_tasks.c.origin_task_id==source['id'])) is None
    account=conn.execute(select(t.users).where(t.users.c.id==user)).mappings().one()
    assert account['demo_date'] is None and account['last_assessed_date']==date(2026,11,9)
    assert account['updated_at'].replace(tzinfo=timezone.utc)==at


def test_explicit_close_does_not_move_demo_clock(api_client,settled_demo_user,domain_connection):
    conn,user=domain_connection,settled_demo_user
    fresh=UserRepository(conn).get(UUID(user)).to_mapping()
    Planner(conn,fresh).close_day(date(2026,11,9))
    assert conn.scalar(select(t.users.c.demo_date).where(t.users.c.id==user))==date(2026,11,9)
    assert conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==user))==date(2026,11,9)


def test_exam_boundary_never_cancels_supplement_obligation(api_client,settled_demo_user,domain_connection):
    conn,user=domain_connection,settled_demo_user
    source=conn.execute(select(t.study_tasks).join(t.subjects).where(t.subjects.c.user_id==user)).mappings().first()
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==source['id']).values(
        task_type='remedial',scheduled_date=date(2026,11,10),status='planned'))
    conn.execute(update(t.subjects).where(t.subjects.c.id==source['subject_id']).values(exam_date=date(2026,11,10)))
    fresh=UserRepository(conn).get(UUID(user)).to_mapping()
    Planner(conn,fresh).close_day(date(2026,11,9))
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==source['id']))=='planned'
