from uuid import uuid4
import pytest
from sqlalchemy import func,select,update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_quiz_selection import add_question,pass_row


def create(client,w,scopes,**changes):
    body={'subjectId':w.subject['id'],'scopes':scopes,'difficulty':'high','count':5,'weight':'even',**changes}
    w.conn.commit()
    return client.post('/api/review/custom-quiz',headers={'X-Anonymous-Id':w.user},json=body)


def scope(unit,start=1,end=10,**extra):return {'unitId':unit['id'],'from':start,'to':end,**extra}


def test_balanced_selection_shortage_forced_low_and_safe_set(api_client,progress_world):
    w=progress_world;first=w.unit(1,10);second=w.unit(1,10)
    for unit in (first,second):w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    for _ in range(4):add_question(w,first)
    for _ in range(3):add_question(w,second)
    response=create(api_client,w,[scope(first),scope(second)],count=4)
    assert response.status_code==200,response.text
    data=response.json()['data'];assert data['count']==4 and data['difficulty']=='low' and '1회독' in data['note']
    byunit=dict(w.conn.execute(select(t.questions.c.unit_id,func.count()).join(t.quiz_items,t.quiz_items.c.question_id==t.questions.c.id)
        .where(t.quiz_items.c.quiz_session_id==data['quizId']).group_by(t.questions.c.unit_id)).all())
    assert byunit=={first['id']:2,second['id']:2};w.conn.commit()
    shown=api_client.get('/api/quiz/'+data['quizId'],headers={'X-Anonymous-Id':w.user})
    assert shown.status_code==200,shown.text
    assert all(not {'answer','correct_answer','explanation','evidence'}&q.keys() for q in shown.json()['data']['questions'])
    short=create(api_client,w,[scope(first),scope(second)],count=50).json()['data']
    assert short['count']==7 and '7개' in short['note']


def test_weak_first_and_requested_second_round_difficulty(api_client,progress_world):
    w=progress_world;strong=w.unit(1,10,score=90);weak=w.unit(1,10,score=30)
    for unit in (strong,weak):
        w.task((unit,1,10),done=[(unit,1,10)],status='completed')
        pass_row(w,unit,1);pass_row(w,unit,2)
        for _ in range(3):add_question(w,unit,difficulty='mid')
    response=create(api_client,w,[scope(strong),scope(weak)],count=2,weight='weak',difficulty='mid')
    assert response.status_code==200,response.text
    data=response.json()['data'];assert data['difficulty']=='mid'
    actual=set(w.conn.scalars(select(t.questions.c.unit_id).join(t.quiz_items,t.quiz_items.c.question_id==t.questions.c.id)
        .where(t.quiz_items.c.quiz_session_id==data['quizId'])))
    assert actual=={weak['id']}


@pytest.mark.parametrize('bad',['duplicate','overlap','unlearned','excluded','quizless','foreign','count0','count51','bool'])
def test_invalid_scopes_never_create_quiz(api_client,progress_world,bad):
    w=progress_world;unit=w.unit(1,10);w.task((unit,1,10),done=[(unit,1,5)],status='partial');add_question(w,unit)
    scopes=[scope(unit,1,5)];changes={}
    if bad=='duplicate':scopes*=2
    elif bad=='overlap':scopes.append(scope(unit,3,5))
    elif bad=='unlearned':scopes=[scope(unit)]
    elif bad=='excluded':w.conn.execute(update(t.units).where(t.units.c.id==unit['id']).values(in_scope=False))
    elif bad=='quizless':w.conn.execute(update(t.materials).where(t.materials.c.id==unit['material_id']).values(quiz_enabled=False))
    elif bad=='foreign':scopes[0]['unitId']=str(uuid4())
    elif bad=='count0':changes['count']=0
    elif bad=='count51':changes['count']=51
    else:changes['count']=True
    before=w.conn.scalar(select(func.count()).select_from(t.quiz_sessions))
    response=create(api_client,w,scopes,**changes)
    assert response.status_code==(404 if bad=='foreign' else 422),response.text
    if bad=='foreign':assert response.json()['error']['code']=='NO_UNITS'
    assert w.conn.scalar(select(func.count()).select_from(t.quiz_sessions))==before


def test_concept_intersection_dedup_and_empty_bank(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10);concept=w.concept(unit,1,5);w.task((unit,1,5),done=[(unit,1,5)],status='completed')
    q=add_question(w,unit,page=3,concept_id=concept['id'])
    response=create(api_client,w,[scope(unit,1,10,conceptId=concept['id']),scope(unit,1,5)],count=5)
    assert response.status_code==200,response.text
    assert response.json()['data']['count']==1
    w.conn.execute(update(t.question_sources).where(t.question_sources.c.question_id==q).values(quote=''));w.conn.commit()
    response=create(api_client,w,[scope(unit,1,5)])
    assert response.status_code==404 and response.json()['error']['code']=='NO_QUESTIONS'


def test_custom_finalized_answer_updates_mastery_once(api_client,progress_world):
    w=progress_world;unit=w.unit(1,5);w.task((unit,1,5),done=[(unit,1,5)],status='completed');add_question(w,unit)
    data=create(api_client,w,[scope(unit,1,5)],count=1).json()['data'];headers={'X-Anonymous-Id':w.user}
    quiz=api_client.get('/api/quiz/'+data['quizId'],headers=headers).json()['data']
    body={'quizId':data['quizId'],'answers':[{'questionId':quiz['questions'][0]['id'],'text':'정답','choice':None,'confidence':'sure'}]}
    first=api_client.post('/api/quiz/'+data['quizId']+'/submit',headers=headers,json=body)
    assert first.status_code==200,first.text
    assert first.json()['data']['unitChanges'][0]['unitId']==unit['id']
    assert api_client.post('/api/quiz/'+data['quizId']+'/submit',headers=headers,json=body).json()==first.json()
    assert w.conn.scalar(select(func.count()).select_from(t.mastery_history).where(t.mastery_history.c.quiz_session_id==data['quizId']))==1


def test_foreign_subject_uses_documented_error(api_client,progress_world):
    w=progress_world;unit=w.unit(1,5)
    response=create(api_client,w,[scope(unit,1,5)],subjectId=str(uuid4()))
    assert response.status_code==404 and response.json()['error']['code']=='SUBJECT_NOT_FOUND'


def test_weak_tie_uses_unit_id_before_material_id(api_client,progress_world):
    w=progress_world
    materials=sorted([w.unit(1,10)['material_id'],w.unit(1,10)['material_id']])
    first=w.unit(1,10,material_id=materials[1],id='11111111-1111-4111-8111-111111111111',sort_order=0,score=30)
    second=w.unit(1,10,material_id=materials[0],id='eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',sort_order=0,score=30)
    for unit in (first,second):
        w.task((unit,1,10),done=[(unit,1,10)],status='completed')
        add_question(w,unit)
    response=create(api_client,w,[scope(second),scope(first)],count=1,weight='weak')
    assert response.status_code==200,response.text
    selected=w.conn.scalar(select(t.questions.c.unit_id).join(t.quiz_items,t.quiz_items.c.question_id==t.questions.c.id)
        .where(t.quiz_items.c.quiz_session_id==response.json()['data']['quizId']))
    assert selected==first['id']
