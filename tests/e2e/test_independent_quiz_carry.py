"""Independent day-close boundary: saved answers, deferred grading and replay."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func,select,update
from app.core.responses import ApiError
from app.db import schema as t
from app.services import quiz_finalization as finalization
from app.services.grading import GradingService
from tests.test_quiz_answer_api import answer_case,DAY,NOW
from test_grading_worker import gateway_factory


def save(client,c,kind,*,text=None,choice=None):
    return client.put(f"/api/quiz/{c['quiz']}/answers/{c[kind+'_item']}",headers={'X-Anonymous-Id':c['user']},
        json={'choice':choice,'text':text,'confidence':'sure','revision':0})


def result(client,c):
    return client.get(f"/api/quiz/{c['quiz']}/result",headers={'X-Anonymous-Id':c['user']})


def close(conn,c):
    user=dict(conn.execute(select(t.users).where(t.users.c.id==c['user'])).mappings().one())
    finalization.close_daily_quizzes(conn,user,DAY,at=NOW+timedelta(seconds=20))


def grade(conn,c,*,failure=False):
    from app.db.session import engine
    ids=list(conn.scalars(select(t.quiz_grading_jobs.c.id).join(t.quiz_answers,t.quiz_answers.c.id==t.quiz_grading_jobs.c.answer_id)
        .join(t.quiz_items,t.quiz_items.c.id==t.quiz_answers.c.quiz_item_id).where(t.quiz_items.c.quiz_session_id==c['quiz'])))
    conn.commit()
    worker=GradingService(engine,owner='independent-carry',clock=lambda:NOW+timedelta(seconds=10),gateway_factory=gateway_factory(failure=failure))
    for identifier in ids:worker.run_job(c['user'],identifier)


def facts(conn,c,table):
    if table is t.mastery_history:return conn.execute(select(table).where(table.c.quiz_session_id==c['quiz'])).all()
    return conn.execute(select(table).join(t.quiz_items,table.c.quiz_item_id==t.quiz_items.c.id)
        .where(t.quiz_items.c.quiz_session_id==c['quiz']).order_by(table.c.id)).all()


@pytest.mark.parametrize('pending',['queued','failed','stale_success'])
def test_partial_close_preserves_answer_heads_until_current_grades_can_finalize(api_client,domain_connection,answer_case,monkeypatch,pending):
    c,conn=answer_case,domain_connection
    monkeypatch.setattr(finalization,'now_utc',lambda:NOW+timedelta(seconds=30))
    assert save(api_client,c,'choice',choice=0).status_code==200
    assert save(api_client,c,'essay',text='작성한 원문\n보존').status_code==200
    if pending!='queued':grade(conn,c,failure=pending=='failed')
    if pending=='stale_success':conn.execute(update(t.question_sources).where(t.question_sources.c.question_id==c['essay']).values(quote='새 근거'))
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==c['quiz']).values(type='daily'))
    original=facts(conn,c,t.quiz_answers)
    close(conn,c);conn.commit()
    first=result(api_client,c)
    assert first.status_code==503,first.text
    assert '비공개 해설' not in first.text and '새 근거' not in first.text and '정답' not in first.text
    assert facts(conn,c,t.quiz_attempts)==[] and facts(conn,c,t.mastery_history)==[]
    state=conn.execute(select(t.quiz_submission_states).where(t.quiz_submission_states.c.quiz_session_id==c['quiz'])).mappings().one()
    assert {v['questionId'] for v in state['sealed_answers']}=={c['choice_item'],c['essay_item']}
    assert state['result_snapshot'] is None
    assert conn.scalar(select(func.count()).select_from(t.quiz_items).where(t.quiz_items.c.quiz_session_id==c['quiz']))==4
    conn.commit()
    assert save(api_client,c,'short',text='늦은 답').status_code==409
    grade(conn,c)
    finished=result(api_client,c);assert finished.status_code==200,finished.text
    assert {r['questionId'] for r in finished.json()['data']['items']}=={c['choice_item'],c['essay_item']}
    assert finished.json()['data']['summary']['total']==2
    assert facts(conn,c,t.quiz_answers)==original
    attempts=facts(conn,c,t.quiz_attempts);history=facts(conn,c,t.mastery_history)
    assert len(attempts)==2 and len(history)==1
    conn.execute(update(t.users).where(t.users.c.id==c['user']).values(demo_date=DAY+timedelta(days=7)))
    conn.execute(update(t.units).where(t.units.c.id==c['unit']).values(mastery_score=1));conn.commit()
    assert result(api_client,c).json()==finished.json()
    assert facts(conn,c,t.quiz_attempts)==attempts and facts(conn,c,t.mastery_history)==history


def test_two_concurrent_closures_finalize_the_saved_subset_only_once(api_client,domain_connection,answer_case,monkeypatch):
    from app.db.session import engine
    c,conn=answer_case,domain_connection
    monkeypatch.setattr(finalization,'now_utc',lambda:NOW+timedelta(seconds=30))
    assert save(api_client,c,'choice',choice=0).status_code==200
    grade(conn,c)
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==c['quiz']).values(type='daily'));conn.commit()
    barrier=Barrier(2)
    def attempt():
        barrier.wait(timeout=10)
        try:
            with engine.begin() as own:close(own,c)
            return 'closed'
        except ApiError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:attempt(),range(2)))
    assert set(outcomes)<={'closed','REQUEST_IN_PROGRESS'} and 'closed' in outcomes
    close(conn,c);conn.commit()
    response=result(api_client,c);assert response.status_code==200,response.text
    assert response.json()['data']['summary']['total']==1
    assert len(facts(conn,c,t.quiz_attempts))==1 and len(facts(conn,c,t.mastery_history))==1
    assert conn.scalar(select(func.count()).select_from(t.quiz_submission_states).where(t.quiz_submission_states.c.quiz_session_id==c['quiz']))==1
    conn.commit();assert result(api_client,c).json()==response.json()


from uuid import uuid4
from tests.test_progress_contract import progress_world
from tests.test_quiz_selection import add_question


def test_next_daily_set_carries_only_unanswered_with_original_item_and_date(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    # Retain the real plan but isolate the prior daily set being tested.
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.user_id==w.user).values(type='practice'))
    qids=[add_question(w,unit) for _ in range(7)]
    quiz,answered,unanswered=(str(uuid4()) for _ in range(3));yesterday=w.day-timedelta(days=1)
    w.conn.execute(t.quiz_sessions.insert().values(id=quiz,user_id=w.user,subject_id=w.subject['id'],type='practice',
        status='ready',quiz_date=yesterday,selected_difficulty='low',created_at=w.now-timedelta(days=1)))
    for position,(item,qid) in enumerate(zip((answered,unanswered),qids[:2]),1):
        w.conn.execute(t.quiz_items.insert().values(id=item,quiz_session_id=quiz,question_id=qid,sort_order=position,inclusion_reason='new'))
    w.conn.commit()
    saved=api_client.put(f'/api/quiz/{quiz}/answers/{answered}',headers={'X-Anonymous-Id':w.user},
        json={'choice':None,'text':'원본 미채점 답','confidence':'sure','revision':0})
    assert saved.status_code==200,saved.text
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==quiz).values(type='daily'))
    user=dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())
    finalization.close_daily_quizzes(w.conn,user,yesterday,at=w.now+timedelta(days=7));w.conn.commit()
    response=api_client.get(f"/api/quiz/{w.subject['id']}",headers={'X-Anonymous-Id':w.user})
    assert response.status_code==200,response.text
    data=response.json()['data']
    rows=w.conn.execute(select(t.quiz_items).where(t.quiz_items.c.quiz_session_id==data['quizId'])).mappings().all()
    assert len(rows)==6 and qids[0] not in {r['question_id'] for r in rows}
    carried=[r for r in rows if r['inclusion_reason']=='carried_over']
    assert len(carried)==1
    assert (carried[0]['question_id'],carried[0]['origin_quiz_item_id'],carried[0]['original_quiz_date'])==(qids[1],unanswered,yesterday)
    assert len({r['question_id'] for r in rows})==6
    original=list(w.conn.scalars(select(t.quiz_items.c.id).where(t.quiz_items.c.quiz_session_id==quiz)))
    assert set(original)=={answered,unanswered}
    w.conn.commit()
    assert api_client.get(f"/api/quiz/{w.subject['id']}",headers={'X-Anonymous-Id':w.user}).json()==response.json()
