"""A new source revision cannot reuse unissued questions from the old PDF."""
from sqlalchemy import select, update

from app.db import schema as t
from tests.e2e.test_independent_question_bank import material_case, bank_world, count, quiz


def test_previous_analysis_bank_is_retained_but_not_newly_issued(api_client, domain_connection, bank_world):
    conn, w = domain_connection, bank_world
    assert w['worker'].run_next()
    before = conn.execute(select(t.questions).order_by(t.questions.c.id)).all()
    assert len(before) == 10
    conn.execute(update(t.materials).where(t.materials.c.id == w['materialId']).values(analysis_version='replacement-analysis-v2'))
    conn.commit()
    result = quiz(api_client, w)
    assert result.status_code == 404 and result.json()['error']['code'] == 'NO_QUESTIONS', result.text
    assert count(conn, t.quiz_sessions) == 0
    assert conn.execute(select(t.questions).order_by(t.questions.c.id)).all() == before
    assert w['gateway'].calls == 1


def test_bank_cannot_borrow_generation_job_owned_by_another_user(api_client, domain_connection, bank_world):
    conn, w = domain_connection, bank_world
    assert w['worker'].run_next()
    job_id = conn.scalar(select(t.ai_jobs.c.id).where(t.ai_jobs.c.kind == 'question_pregenerate'))
    conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job_id).values(user_id=w['other_user']))
    conn.commit()
    result = quiz(api_client, w)
    assert result.status_code == 404 and result.json()['error']['code'] == 'NO_QUESTIONS', result.text
    assert count(conn, t.quiz_sessions) == 0 and count(conn, t.questions) == 10
    assert w['gateway'].calls == 1
