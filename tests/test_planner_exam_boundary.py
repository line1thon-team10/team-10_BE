from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.db.schema import study_records, study_tasks, subjects, users
from app.repositories.users import UserRepository
from app.services.planner import Planner


@pytest.mark.parametrize("exam", [date(2026,11,9), date(2026,11,10)])
def test_day_closure_does_not_carry_into_or_after_exam(api_client, settled_demo_user, domain_connection, exam):
    conn = domain_connection
    subject_id = conn.scalar(select(subjects.c.id).where(subjects.c.user_id == settled_demo_user, subjects.c.code == "AI"))
    conn.execute(update(subjects).where(subjects.c.id == subject_id).values(exam_date=exam))
    before_ids = set(conn.scalars(select(study_tasks.c.id).where(study_tasks.c.subject_id == subject_id)))
    before_obligations = [dict(row) for row in conn.execute(select(study_tasks).where(
        study_tasks.c.subject_id == subject_id, study_tasks.c.task_type.in_(["remedial", "review"]))).mappings()]
    before_records = conn.scalar(select(func.count()).select_from(study_records))
    user = UserRepository(conn).get(UUID(settled_demo_user))
    Planner(conn, user.to_mapping()).advance_day()
    after_ids = set(conn.scalars(select(study_tasks.c.id).where(study_tasks.c.subject_id == subject_id)))
    assert before_ids == after_ids
    assert conn.scalar(select(func.count()).select_from(study_tasks).where(
        study_tasks.c.subject_id == subject_id, study_tasks.c.scheduled_date >= exam, study_tasks.c.task_type == "study",
        study_tasks.c.status.in_(["planned","partial"]))) == 0
    assert [dict(row) for row in conn.execute(select(study_tasks).where(
        study_tasks.c.subject_id == subject_id, study_tasks.c.task_type.in_(["remedial", "review"]))).mappings()] == before_obligations
    assert conn.scalar(select(func.count()).select_from(study_records)) == before_records
    assert conn.scalar(select(users.c.demo_date).where(users.c.id == settled_demo_user)) == date(2026,11,10)
    assert conn.scalar(select(users.c.last_assessed_date).where(users.c.id == settled_demo_user)) == date(2026,11,9)
