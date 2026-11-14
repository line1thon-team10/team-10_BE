from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_quiz_answer_api import answer_case, make_case, DAY, NOW


def weak(conn,case,kind='choice',*,correct=False,confidence='sure',day=DAY,due=None,step=0,status='active'):
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==case['quiz']).values(status='submitted',grading_status='completed',finalized_at=NOW))
    conn.execute(t.quiz_attempts.insert().values(id=str(uuid4()),quiz_item_id=case[kind+'_item'],answer='secret raw',confidence=confidence,
        is_correct=correct,grading_status='graded',essay_score=40 if kind=='essay' else None,
        answered_at=NOW,answered_date=day,finalized_at=NOW,graded_at=NOW))
    conn.execute(t.weak_questions.insert().values(question_id=case[kind],status=status,review_step=step,
        next_review_date=due or day+timedelta(days=1),last_weak_at=NOW))
    conn.commit()


def notes(client,case,group='date'):
    return client.get('/api/review/notes',headers={'X-Anonymous-Id':case['user']},params={'group':group})


def test_empty_and_required_group(api_client,answer_case):
    response=notes(api_client,answer_case)
    assert response.status_code==200,response.text
    assert response.json()['data']=={'group':'date','total':0,'groups':[],'weakConcepts':[]}
    assert notes(api_client,answer_case,'other').status_code==422
    assert api_client.get('/api/review/notes',headers={'X-Anonymous-Id':answer_case['user']}).status_code==422


@pytest.mark.parametrize('group',['date','unit','count'])
def test_owned_history_order_and_due_state(api_client,domain_connection,answer_case,group):
    case,conn=answer_case,domain_connection
    weak(conn,case,kind='choice',day=DAY-timedelta(days=1),due=DAY)
    weak(conn,case,kind='essay',day=DAY,due=DAY+timedelta(days=1))
    response=notes(api_client,case,group)
    assert response.status_code==200,response.text
    data=response.json()['data'];rows={n['id']:n for g in data['groups'] for n in g['items']}
    assert data['total']==2 and set(rows)=={case['choice'],case['essay']}
    assert rows[case['choice']]['canRetry'] and not rows[case['essay']]['canRetry']
    assert rows[case['choice']]['materialId']==case['material'] and rows[case['choice']]['unitId']==case['unit']
    assert rows[case['essay']]['score']==40 and rows[case['choice']]['score'] is None
    assert rows[case['choice']]['history']==[{'date':(DAY-timedelta(days=1)).isoformat(),'result':'sureWrong','score':None}]
    if group=='date':assert [g['key'] for g in data['groups']]==[DAY.isoformat(),(DAY-timedelta(days=1)).isoformat()]
    if group=='unit':assert data['groups'][0]['key']==case['unit']


def test_question_never_leaks_solution_and_foreign_note_is_hidden(api_client,domain_connection,answer_case):
    case,conn=answer_case,domain_connection;weak(conn,case)
    other=make_case(conn);weak(conn,other)
    response=api_client.get(f"/api/review/notes/{case['choice']}/question",headers={'X-Anonymous-Id':case['user']})
    assert response.status_code==200,response.text
    assert response.json()['data']=={'noteId':case['choice'],'type':'choice','prompt':'choice','options':['맞음','틀림'],'mockFilled':None}
    assert 'secret raw' not in response.text and '해설' not in response.text and '근거' not in response.text
    missing=api_client.get(f"/api/review/notes/{other['choice']}/question",headers={'X-Anonymous-Id':case['user']})
    assert missing.status_code==404 and missing.json()['error']['code']=='NO_QUESTION'
    assert notes(api_client,case).json()['data']['total']==1


def test_unfinalized_attempt_never_creates_visible_note(api_client,domain_connection,answer_case):
    weak(domain_connection,answer_case)
    domain_connection.execute(update(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id==answer_case['choice_item']).values(finalized_at=None))
    domain_connection.commit()
    assert notes(api_client,answer_case).json()['data']['total']==0


@pytest.mark.parametrize('corrupt',['foreign_concept','foreign_subject','ungraded_set','future_attempt'])
def test_note_projection_rejects_unrelated_or_unconfirmed_facts(api_client,domain_connection,answer_case,corrupt):
    conn,case=domain_connection,answer_case;weak(conn,case)
    other=make_case(conn)
    if corrupt=='foreign_concept':
        cid=str(uuid4());conn.execute(t.concepts.insert().values(id=cid,unit_id=other['unit'],code='foreign',title='foreign',
            sort_order=1,start_page=1,end_page=1,in_scope=True,created_at=NOW,updated_at=NOW))
        conn.execute(update(t.questions).where(t.questions.c.id==case['choice']).values(concept_id=cid))
    elif corrupt=='foreign_subject':conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==case['quiz']).values(subject_id=other['subject']))
    elif corrupt=='ungraded_set':conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==case['quiz']).values(grading_status='failed'))
    else:conn.execute(update(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id==case['choice_item']).values(answered_date=DAY+timedelta(days=1)))
    conn.commit()
    assert notes(api_client,case).json()['data']['total']==0
    assert api_client.get(f"/api/review/notes/{case['choice']}/question",headers={'X-Anonymous-Id':case['user']}).status_code==404


def test_same_display_number_keeps_pdf_identity_and_history_order(api_client,domain_connection,answer_case):
    conn,case=domain_connection,answer_case
    cid=str(uuid4());conn.execute(t.concepts.insert().values(id=cid,unit_id=case['unit'],code='c',title='동일 개념',sort_order=1,
        start_page=1,end_page=1,in_scope=True,created_at=NOW,updated_at=NOW))
    conn.execute(update(t.questions).where(t.questions.c.id==case['choice']).values(concept_id=cid))
    weak(conn,case,day=DAY-timedelta(days=3),due=DAY)
    material,unit,question,concept=[str(uuid4()) for _ in range(4)]
    source=dict(conn.execute(select(t.materials).where(t.materials.c.id==case['material'])).mappings().one())
    conn.execute(t.materials.insert().values(**{**source,'id':material,'sha256':'second-pdf'}))
    source=dict(conn.execute(select(t.units).where(t.units.c.id==case['unit'])).mappings().one())
    conn.execute(t.units.insert().values(**{**source,'id':unit,'material_id':material}))
    conn.execute(t.concepts.insert().values(id=concept,unit_id=unit,code='c',title='동일 개념',sort_order=1,
        start_page=1,end_page=1,in_scope=True,created_at=NOW,updated_at=NOW))
    source=dict(conn.execute(select(t.questions).where(t.questions.c.id==case['choice'])).mappings().one())
    conn.execute(t.questions.insert().values(**{**source,'id':question,'unit_id':unit,'concept_id':concept}))
    for qid,correct,confidence,day in [(case['choice'],False,'unsure',DAY-timedelta(days=1)),(question,True,'unsure',DAY)]:
        quiz,item=str(uuid4()),str(uuid4())
        conn.execute(t.quiz_sessions.insert().values(id=quiz,user_id=case['user'],subject_id=case['subject'],type='review',quiz_date=day,
            status='submitted',grading_status='completed',finalized_at=NOW,created_at=NOW))
        conn.execute(t.quiz_items.insert().values(id=item,quiz_session_id=quiz,question_id=qid,sort_order=1,inclusion_reason='weak'))
        conn.execute(t.quiz_attempts.insert().values(id=str(uuid4()),quiz_item_id=item,answer='stored',confidence=confidence,is_correct=correct,
            grading_status='graded',answered_at=NOW,answered_date=day,finalized_at=NOW,graded_at=NOW))
    conn.execute(t.weak_questions.insert().values(question_id=question,status='active',review_step=0,next_review_date=DAY+timedelta(days=1),last_weak_at=NOW))
    conn.commit()
    units=notes(api_client,case,'unit').json()['data']
    assert {g['key'] for g in units['groups']}=={unit,case['unit']}
    assert {n['unitNo'] for g in units['groups'] for n in g['items']}=={1}
    count=notes(api_client,case,'count').json()['data']
    assert [g['key'] for g in count['groups']]==['2','0']
    history=count['groups'][0]['items'][0]['history']
    assert [h['date'] for h in history]==[(DAY-timedelta(days=3)).isoformat(),(DAY-timedelta(days=1)).isoformat()]
    assert count['weakConcepts']==[{'subjectId':case['subject'],'subjectName':'자료','materialId':case['material'],
        'unitId':case['unit'],'conceptId':cid,'concept':'동일 개념','count':2}]
