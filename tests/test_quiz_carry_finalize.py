from datetime import timedelta
import pytest
from sqlalchemy import func, select, update
from app.db import schema as t
from app.services import quiz_finalization as finalization
from app.services.grading import GradingService
from app.repositories.quiz_answers import QuizAnswerRepository
from tests.test_quiz_answer_api import NOW, DAY, answer_case, put
from tests.test_quiz_finalize import facts, get_result
from test_grading_worker import gateway_factory


@pytest.fixture
def carry_case(api_client,answer_case,domain_connection,monkeypatch):
    monkeypatch.setattr(finalization,'now_utc',lambda:NOW+timedelta(seconds=30))
    return answer_case


def close(conn,case):
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==case['quiz']).values(type='daily'))
    user=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    finalization.close_daily_quizzes(conn,user,DAY,at=NOW+timedelta(seconds=20))
    conn.commit()


def worker_jobs(conn,case,*,failure=False,retry=False):
    from app.db.session import engine
    ids=list(conn.scalars(select(t.quiz_grading_jobs.c.id).join(t.quiz_answers,t.quiz_answers.c.id==t.quiz_grading_jobs.c.answer_id)
        .join(t.quiz_items,t.quiz_items.c.id==t.quiz_answers.c.quiz_item_id).where(t.quiz_items.c.quiz_session_id==case['quiz'])))
    conn.commit()
    worker=GradingService(engine,owner='carry-test',clock=lambda:NOW+timedelta(seconds=10),
        gateway_factory=gateway_factory(failure=failure,score=85))
    return [worker.run_job(case['user'],identifier,retry_failed=retry) for identifier in ids]


def test_only_answered_original_items_finalize_and_replay_once(api_client,domain_connection,carry_case):
    case,conn=carry_case,domain_connection
    assert put(api_client,case,'choice').status_code==200
    assert put(api_client,case,'short',text='맞음').status_code==200
    worker_jobs(conn,case)
    close(conn,case)
    response=get_result(api_client,case);assert response.status_code==200,response.text
    result=response.json()['data'];assert result['summary']['total']==2 and result['summary']['correct']==2
    assert {item['questionId'] for item in result['items']}=={case['choice_item'],case['short_item']}
    quiz=conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.id==case['quiz'])).mappings().one()
    assert quiz['status']=='carried_over' and quiz['submitted_at'] is None and quiz['grading_status']=='completed'
    before=facts(conn,case);assert len(before['attempts'])==2 and len(before['history'])==1
    conn.commit();close(conn,case)
    assert get_result(api_client,case).json()==response.json()
    assert facts(conn,case)==before
    conn.commit()
    refused=put(api_client,case,'essay',text='뒤늦은 새 답')
    assert refused.status_code==409 and refused.json()['error']['code']=='QUIZ_FINALIZED'


def test_pending_and_failed_grading_preserve_inputs_until_original_result_can_publish(api_client,domain_connection,carry_case):
    case,conn=carry_case,domain_connection
    assert put(api_client,case,'choice').status_code==200
    assert put(api_client,case,'essay',text='보존할 답안').status_code==200
    worker_jobs(conn,case,failure=True)
    close(conn,case)
    assert get_result(api_client,case).status_code==503
    assert facts(conn,case)=={'attempts':[],'history':[]}
    original=QuizAnswerRepository(conn).list_current(case['user'],case['quiz'])
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=DAY+timedelta(days=1)))
    conn.commit()
    worker_jobs(conn,case,retry=True)
    response=get_result(api_client,case);assert response.status_code==200,response.text
    assert response.json()['data']['summary']['total']==2
    assert QuizAnswerRepository(conn).list_current(case['user'],case['quiz'])==original
    attempts=conn.execute(select(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id.in_([case['choice_item'],case['essay_item']]))).mappings().all()
    assert len(attempts)==2 and {a['answered_date'] for a in attempts}=={DAY}
    before=facts(conn,case);conn.commit()
    assert get_result(api_client,case).json()==response.json()
    assert facts(conn,case)==before


def test_zero_answer_day_close_creates_no_grade_or_snapshot(api_client,domain_connection,carry_case):
    case,conn=carry_case,domain_connection
    close(conn,case)
    assert get_result(api_client,case).status_code==404
    assert facts(conn,case)=={'attempts':[],'history':[]}
    assert QuizAnswerRepository(conn).submission(case['user'],case['quiz']) is None
    assert conn.scalar(select(func.count()).select_from(t.quiz_items).where(t.quiz_items.c.quiz_session_id==case['quiz']))==4
