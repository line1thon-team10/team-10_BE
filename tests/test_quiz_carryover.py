from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_quiz_selection import add_question, get, prior_seed_quizzes


def previous(w,unit,qids,day,*,answered=False,confidence='sure',correct=False,reason='new'):
    quiz=str(uuid4());items=[]
    w.conn.execute(t.quiz_sessions.insert().values(id=quiz,user_id=w.user,subject_id=w.subject['id'],type='daily',
        quiz_date=day,status='carried_over',selected_difficulty='low',created_at=w.now-timedelta(days=1),finalized_at=w.now))
    for i,qid in enumerate(qids):
        item=str(uuid4());items.append(item)
        w.conn.execute(t.quiz_items.insert().values(id=item,quiz_session_id=quiz,question_id=qid,sort_order=i+1,inclusion_reason=reason))
        if answered:
            w.conn.execute(t.quiz_attempts.insert().values(id=str(uuid4()),quiz_item_id=item,answer='답',confidence=confidence,
                is_correct=correct,grading_status='graded',answered_at=w.now,answered_date=day,finalized_at=w.now,graded_at=w.now))
    return quiz,items


def test_unanswered_chain_moves_once_but_answered_question_is_not_duplicated(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    questions=[add_question(w,unit) for _ in range(7)]
    yesterday=w.day-timedelta(days=1)
    old,items=previous(w,unit,questions[:2],yesterday)
    from app.repositories.quiz_answers import QuizAnswerRepository
    # Already answered but pending grading is never copied as "unanswered".
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==old).values(status='in_progress',finalized_at=None))
    QuizAnswerRepository(w.conn).save(w.user,old,items[0],revision=0,text='답',choice=None,confidence='sure',now=w.now,answered_date=yesterday)
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==old).values(status='carried_over',finalized_at=w.now))
    response=get(api_client,w);assert response.status_code==200,response.text
    quiz=response.json()['data']['quizId']
    rows=w.conn.execute(select(t.quiz_items).where(t.quiz_items.c.quiz_session_id==quiz)).mappings().all()
    assert len(rows)==6
    carried=[r for r in rows if r['inclusion_reason']=='carried_over']
    assert len(carried)==1 and carried[0]['question_id']==questions[1]
    assert carried[0]['origin_quiz_item_id']==items[1] and carried[0]['original_quiz_date']==yesterday
    assert questions[0] not in {r['question_id'] for r in rows}
    assert get(api_client,w).json()==response.json()


@pytest.mark.parametrize('weekday',[0,5,6])
def test_due_weak_questions_have_no_arbitrary_count_limit(api_client,progress_world,weekday):
    w=progress_world
    w.day=w.day+timedelta(days=weekday)
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=w.day))
    unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    old=[add_question(w,unit) for _ in range(105)]
    previous(w,unit,old,w.day-timedelta(days=1),answered=True)
    for qid in old:w.conn.execute(t.weak_questions.insert().values(question_id=qid,status='active',review_step=0,
        next_review_date=w.day-timedelta(days=1),last_weak_at=w.now))
    fresh={add_question(w,unit) for _ in range(5)}
    w.conn.commit()
    cards=api_client.get('/api/quiz/today',headers={'X-Anonymous-Id':w.user}).json()['data']['subjects']
    assert next(row['count'] for row in cards if row['subjectId']==w.subject['id'])==110
    response=get(api_client,w);assert response.status_code==200,response.text
    result=response.json()['data'];assert len(result['questions'])==110
    rows=w.conn.execute(select(t.quiz_items).where(t.quiz_items.c.quiz_session_id==result['quizId'])).mappings().all()
    assert len({r['question_id'] for r in rows})==110
    assert {r['question_id'] for r in rows if r['inclusion_reason']=='new'}==fresh


def test_weekend_review_includes_weekly_wrong_even_before_next_scheduled_review(api_client,progress_world):
    w=progress_world;w.day+=timedelta(days=5)
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=w.day))
    unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    q=add_question(w,unit);previous(w,unit,[q],w.day-timedelta(days=4),answered=True)
    w.conn.execute(t.weak_questions.insert().values(question_id=q,status='active',review_step=1,
        next_review_date=w.day+timedelta(days=1),last_weak_at=w.now))
    for _ in range(5):add_question(w,unit)
    response=get(api_client,w);assert response.status_code==200,response.text
    assert len(response.json()['data']['questions'])==6
