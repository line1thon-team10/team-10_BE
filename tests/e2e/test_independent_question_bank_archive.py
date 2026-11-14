"""Archiving a subject fences admission and publication of bank work."""
import pytest
from sqlalchemy import select, update

from app.db import schema as t, session
from tests.e2e.test_independent_question_bank import material_case, bank_world, count


@pytest.mark.parametrize('during_generation', [False, True])
def test_archived_subject_never_receives_new_bank_questions(api_client, domain_connection, bank_world, during_generation):
    conn, w = domain_connection, bank_world
    def archive():
        with session.engine.begin() as writer:
            writer.execute(update(t.subjects).where(t.subjects.c.id == w['subject']).values(status='archived'))
    if during_generation:
        w['gateway'].callback = archive
        assert w['worker'].run_next()
        job = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.kind == 'question_pregenerate')).mappings().one()
        assert job['status'] == 'failed' and job['error_code'] == 'QUESTION_BANK_STALE'
    else:
        archive()
        assert not w['worker'].run_next()
        assert w['gateway'].calls == 0
    assert count(conn, t.questions) == 0
    assert conn.scalar(select(t.materials.c.analysis_status)) == 'succeeded'
