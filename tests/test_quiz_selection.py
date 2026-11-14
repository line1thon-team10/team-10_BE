"""HTTP selection uses real completed ranges, immutable item IDs and DB state."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import func, select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world


@pytest.fixture(autouse=True)
def prior_seed_quizzes(progress_world):
    w=progress_world
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.user_id==w.user).values(quiz_date=w.day-timedelta(days=1)))
    w.conn.commit()


def add_question(w, unit, *, page=1, difficulty='low', **changes):
    qid=str(uuid4())
    values=dict(id=qid,unit_id=unit['id'],type='short',difficulty=difficulty,prompt='검증된 문항',
        correct_answer='정답',explanation='원문 설명',origin='pregenerated',code=qid,created_at=w.now)
    values.update(changes)
    w.conn.execute(t.questions.insert().values(**values))
    w.conn.execute(t.question_sources.insert().values(id=str(uuid4()),question_id=qid,
        material_id=unit['material_id'],start_page=page,end_page=page,quote='실제 근거'))
    return qid


def get(client,w,**params):
    w.conn.commit()
    return client.get(f"/api/quiz/{w.subject['id']}",headers={'X-Anonymous-Id':w.user},params=params)


def pass_row(w,unit,number,*,invalid=False):
    w.conn.execute(t.learning_passes.insert().values(id=str(uuid4()),unit_id=unit['id'],pass_number=number,
        completion_kind='full_relearn',evidence_key=str(uuid4()),evidence_snapshot={'schemaVersion':'1','recordIds':[],
        'requiredRanges':[]},completed_at=w.now,completed_date=w.day,invalidated_at=w.now if invalid else None))


def test_partial_completion_selects_exactly_five_and_keeps_answers_private(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,7)],status='partial')
    good={add_question(w,unit,page=7) for _ in range(5)}
    bad={add_question(w,unit,page=8) for _ in range(7)}
    response=get(api_client,w,difficulty='high')
    assert response.status_code==200,response.text
    data=response.json()['data'];assert data['difficulty']=='하' and len(data['questions'])==5
    actual=set(w.conn.scalars(select(t.quiz_items.c.question_id).where(t.quiz_items.c.quiz_session_id==data['quizId'])))
    assert actual==good and not actual&bad
    assert all(not {'answer','correct_answer','explanation','evidence','page'}&q.keys() for q in data['questions'])
    replay=get(api_client,w,quizId=data['quizId'])
    assert replay.json()==response.json()
    assert get(api_client,w,quizId=data['quizId'],difficulty='high').status_code==409


@pytest.mark.parametrize('count',[0,1,4])
def test_bank_shortage_has_no_empty_or_partial_session(api_client,progress_world,count):
    w=progress_world;unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    for _ in range(count):add_question(w,unit)
    before=w.conn.scalar(select(func.count()).select_from(t.quiz_sessions));w.conn.commit()
    response=get(api_client,w)
    assert response.status_code==404 and response.json()['error']['code']=='NO_QUESTIONS'
    assert w.conn.scalar(select(func.count()).select_from(t.quiz_sessions))==before
    w.conn.commit()
    card=api_client.get('/api/quiz/today',headers={'X-Anonymous-Id':w.user}).json()['data']
    row=next(row for row in card['subjects'] if row['subjectId']==w.subject['id'])
    assert row['count']==0 and row['hasQuestions'] is False


@pytest.mark.parametrize('done,total,enabled',[(6999,10000,True),(0,10,True),(10,10,False)])
def test_raw_threshold_and_quizless_are_closed_without_writes(api_client,progress_world,done,total,enabled):
    w=progress_world;unit=w.unit(1,total,quiz_enabled=enabled)
    w.task((unit,1,total),done=[(unit,1,done)] if done else [],status='partial')
    for _ in range(5):add_question(w,unit)
    response=get(api_client,w)
    assert response.status_code==403 and response.json()['error']['code']=='QUIZ_LOCKED'


def test_round_count_ignores_invalid_history_and_requires_second_round_choice(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    pass_row(w,unit,1);pass_row(w,unit,9,invalid=True)
    for _ in range(5):add_question(w,unit,difficulty='low')
    for _ in range(5):add_question(w,unit,difficulty='mid')
    response=get(api_client,w);assert response.status_code==200,response.text
    assert response.json()['data']['round']=='1회독'
    # A different subject/day starts a new selection after two valid passes.
    pass_row(w,unit,10)
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==response.json()['data']['quizId']).values(quiz_date=w.day-timedelta(days=1)))
    w.conn.commit()
    assert get(api_client,w).status_code==422
    response=get(api_client,w,difficulty='mid')
    assert response.status_code==200,response.text
    assert response.json()['data']['difficulty']=='중' and response.json()['data']['round']=='2회독'


def test_new_completed_unit_appends_without_replacing_saved_answers(api_client,progress_world):
    w=progress_world;first=w.unit(1,10);w.task((first,1,10),done=[(first,1,10)],status='completed')
    for _ in range(5):add_question(w,first)
    initial=get(api_client,w).json()['data'];item=initial['questions'][0]['id']
    saved=api_client.put(f"/api/quiz/{initial['quizId']}/answers/{item}",headers={'X-Anonymous-Id':w.user},
        json={'choice':None,'text':'아직 쓰는 답','confidence':'unsure','revision':0})
    assert saved.status_code==200,saved.text
    second=w.unit(1,10);w.task((second,1,10),done=[(second,1,10)],status='completed')
    for _ in range(5):add_question(w,second)
    response=get(api_client,w)
    assert response.status_code==200,response.text
    result=response.json()['data'];assert result['quizId']==initial['quizId'] and len(result['questions'])==10
    assert result['savedAnswers']==[{'questionId':item,'choice':None,'text':'아직 쓰는 답','confidence':'unsure','revision':1}]
    assert [q['id'] for q in result['questions'][:5]]==[q['id'] for q in initial['questions']]
    assert get(api_client,w).json()==response.json()


def test_submitted_set_is_immutable_and_new_unit_gets_followup_daily(api_client,progress_world):
    w=progress_world;first=w.unit(1,10);w.task((first,1,10),done=[(first,1,10)],status='completed')
    for _ in range(5):add_question(w,first)
    initial=get(api_client,w).json()['data']
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==initial['quizId']).values(status='submitted',finalized_at=w.now))
    second=w.unit(1,10);w.task((second,1,10),done=[(second,1,10)],status='completed')
    for _ in range(5):add_question(w,second)
    response=get(api_client,w);assert response.status_code==200,response.text
    result=response.json()['data'];assert result['quizId']!=initial['quizId'] and len(result['questions'])==5
    assert {q['unitId'] for q in result['questions']}=={second['id']}
    assert w.conn.scalar(select(func.count()).select_from(t.quiz_items).where(t.quiz_items.c.quiz_session_id==initial['quizId']))==5


@pytest.mark.parametrize('invalid',['foreign_source','missing_quote','missing_explanation','foreign_concept','source_beyond_done'])
def test_invalid_bank_evidence_never_becomes_a_selected_item(api_client,progress_world,invalid):
    w=progress_world;unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,7)],status='partial')
    other=w.unit(1,10)
    q=add_question(w,unit)
    if invalid=='foreign_source':w.conn.execute(update(t.question_sources).where(t.question_sources.c.question_id==q).values(material_id=other['material_id']))
    if invalid=='missing_quote':w.conn.execute(update(t.question_sources).where(t.question_sources.c.question_id==q).values(quote=''))
    if invalid=='missing_explanation':w.conn.execute(update(t.questions).where(t.questions.c.id==q).values(explanation=''))
    if invalid=='foreign_concept':
        concept=w.concept(other,1,10)
        w.conn.execute(update(t.questions).where(t.questions.c.id==q).values(concept_id=concept['id']))
    if invalid=='source_beyond_done':w.conn.execute(update(t.question_sources).where(t.question_sources.c.question_id==q).values(end_page=8))
    for _ in range(4):add_question(w,unit)
    response=get(api_client,w)
    assert response.status_code==404 and response.json()['error']['code']=='NO_QUESTIONS'
