from uuid import uuid4
import pytest
from sqlalchemy import func, select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_quiz_selection import add_question, get, prior_seed_quizzes, pass_row


def base(client,w,count=5):
    unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,7)],status='partial')
    for _ in range(count):add_question(w,unit,page=7)
    response=get(client,w);assert response.status_code==200,response.text
    quiz=response.json()['data']['quizId']
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==quiz).values(status='submitted',grading_status='completed',finalized_at=w.now))
    w.conn.commit()
    return unit,quiz


def more(client,w,unit,**params):
    w.conn.commit()
    return client.post(f"/api/quiz/{w.subject['id']}/more",headers={'X-Anonymous-Id':w.user},json={'unitId':unit['id'],**params})


@pytest.mark.parametrize('count',[0,1,4,5,9])
def test_extra_requires_exactly_five_unseen_eligible_questions(api_client,progress_world,count):
    w=progress_world;unit,parent=base(api_client,w,count+5)
    # Plenty of unseen questions beyond completed page 7 are never a fallback.
    for _ in range(6):add_question(w,unit,page=8)
    before=w.conn.scalar(select(func.count()).select_from(t.quiz_sessions))
    response=more(api_client,w,unit)
    if count<5:
        assert response.status_code==404 and response.json()['error']['code']=='NO_QUESTIONS'
        assert w.conn.scalar(select(func.count()).select_from(t.quiz_sessions))==before
    else:
        assert response.status_code==200,response.text
        data=response.json()['data'];assert data['count']==5
        session=w.conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.id==data['quizId'])).mappings().one()
        assert session['parent_quiz_session_id']==parent
        sources=w.conn.execute(select(t.question_sources).join(t.quiz_items,t.quiz_items.c.question_id==t.question_sources.c.question_id)
            .where(t.quiz_items.c.quiz_session_id==data['quizId'])).mappings().all()
        assert len(sources)==5 and all(s['start_page']==7 for s in sources)
        assert more(api_client,w,unit).json()==response.json()
        assert more(api_client,w,unit,difficulty='mid').status_code==409


def test_extra_rejects_owned_unit_absent_from_result_and_foreign_unit(api_client,progress_world):
    w=progress_world;unit,parent=base(api_client,w,10)
    unused=w.unit(1,10)
    assert more(api_client,w,unused).status_code==404
    response=more(api_client,w,{'id':str(uuid4())})
    assert response.status_code==404 and response.json()['error']['code']=='UNIT_NOT_FOUND'


def test_extra_second_round_requires_choice_and_selects_that_difficulty(api_client,progress_world):
    w=progress_world;unit,parent=base(api_client,w,10)
    pass_row(w,unit,1);pass_row(w,unit,3)
    for _ in range(5):add_question(w,unit,page=7,difficulty='high')
    assert more(api_client,w,unit).status_code==422
    response=more(api_client,w,unit,difficulty='high');assert response.status_code==200,response.text
    quiz=response.json()['data']['quizId']
    difficulties=list(w.conn.scalars(select(t.questions.c.difficulty).join(t.quiz_items,t.quiz_items.c.question_id==t.questions.c.id)
        .where(t.quiz_items.c.quiz_session_id==quiz)))
    assert difficulties==['high']*5
    assert more(api_client,w,unit,difficulty='high').json()==response.json()
