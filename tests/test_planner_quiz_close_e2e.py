import pytest
from tests.test_quiz_carry_finalize import worker_jobs
from datetime import timedelta
from uuid import uuid4
"""The public day-close path seals answered quiz items, without invented grades."""
from sqlalchemy import select, update
from app.db import schema as t
from app.repositories.quiz_answers import QuizAnswerRepository
from tests.test_quiz_answer_api import NOW, DAY, answer_case, put


@pytest.mark.parametrize('graded',[False,True])
def test_public_day_close_seals_only_answered_heads(api_client,domain_connection,answer_case,graded):
    case,conn=answer_case,domain_connection
    conn.execute(t.plans.insert().values(id=str(uuid4()),user_id=case['user'],revision=1,created_at=NOW,approved_at=NOW))
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['subject']).values(exam_date=DAY+timedelta(days=3)))
    conn.commit()
    assert put(api_client,case,'choice').status_code==200
    if graded:
        worker_jobs(conn,case)
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==case['quiz']).values(type='daily'))
    conn.commit()
    response=api_client.post('/api/demo/next-day',headers={'X-Anonymous-Id':case['user']})
    assert response.status_code==200,response.text
    quiz=conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.id==case['quiz'])).mappings().one()
    assert quiz['status']=='carried_over'
    state=QuizAnswerRepository(conn).submission(case['user'],case['quiz'])
    assert state is not None and state['answer_set_hash']
    attempts=list(conn.execute(select(t.quiz_attempts).join(t.quiz_items,t.quiz_items.c.id==t.quiz_attempts.c.quiz_item_id)
        .where(t.quiz_items.c.quiz_session_id==case['quiz'])).mappings())
    assert len(attempts)==int(graded)
    if graded:
        assert attempts[0]['quiz_item_id']==case['choice_item']
    else:
        assert state['result_snapshot'] is None
