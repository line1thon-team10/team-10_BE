"""Independent review contract checks through HTTP and durable DB facts."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_quiz_answer_api import answer_case, make_case, DAY, NOW
from tests.test_quiz_selection import add_question


def post(client,user,path,body,key=None):
    headers={'X-Anonymous-Id':user}
    if key is not None:headers['Idempotency-Key']=key
    return client.post(path,headers=headers,json=body)


def seed_note(conn,case):
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==case['quiz']).values(status='submitted',grading_status='completed',finalized_at=NOW))
    conn.execute(t.quiz_attempts.insert().values(id=str(uuid4()),quiz_item_id=case['choice_item'],answer='틀림',
        confidence='unsure',is_correct=False,grading_status='graded',answered_at=NOW,
        answered_date=DAY-timedelta(days=1),finalized_at=NOW,graded_at=NOW))
    conn.execute(t.weak_questions.insert().values(question_id=case['choice'],status='active',review_step=0,
        next_review_date=DAY,last_weak_at=NOW))
    conn.commit()


def test_due_unsure_success_resets_interval_without_replaying_as_new_attempt(api_client,domain_connection,answer_case):
    conn,c=domain_connection,answer_case;seed_note(conn,c)
    path=f"/api/review/notes/{c['choice']}/retry"
    sure={'choice':0,'text':None,'confidence':'sure'}
    first=post(api_client,c['user'],path,sure)
    assert first.status_code==200,first.text
    assert first.json()['data']['note']['streak']==1
    before=conn.scalar(select(func.count()).select_from(t.quiz_attempts));conn.commit()
    unsure={**sure,'confidence':'unsure'}
    early=post(api_client,c['user'],path,unsure)
    assert early.status_code==409 and early.json()['error']['code']=='REVIEW_NOT_DUE'
    assert conn.scalar(select(func.count()).select_from(t.quiz_attempts))==before
    conn.execute(update(t.users).where(t.users.c.id==c['user']).values(demo_date=DAY+timedelta(days=3)));conn.commit()
    second=post(api_client,c['user'],path,unsure)
    assert second.status_code==200,second.text
    note=second.json()['data']['note']
    assert note['streak']==0 and note['result']=='unsureRight'
    assert [h['result'] for h in note['history']]==['unsureWrong','sureRight','unsureRight']
    assert conn.scalar(select(t.weak_questions.c.next_review_date).where(t.weak_questions.c.question_id==c['choice']))==DAY+timedelta(days=4)
    count=conn.scalar(select(func.count()).select_from(t.mastery_history));conn.commit()
    assert post(api_client,c['user'],path,unsure).json()==second.json()
    assert conn.scalar(select(func.count()).select_from(t.mastery_history))==count


def test_retry_original_result_survives_unrelated_score_and_other_owner(api_client,domain_connection,answer_case):
    conn,c=domain_connection,answer_case;seed_note(conn,c)
    path=f"/api/review/notes/{c['choice']}/retry";body={'choice':0,'text':None,'confidence':'sure'}
    response=post(api_client,c['user'],path,body);assert response.status_code==200,response.text
    foreign=make_case(conn)
    conn.execute(update(t.units).where(t.units.c.id==c['unit']).values(mastery_score=7,mastery_level='D'));conn.commit()
    assert post(api_client,c['user'],path,body).json()==response.json()
    before=conn.scalar(select(func.count()).select_from(t.quiz_attempts));conn.commit()
    assert post(api_client,foreign['user'],path,body).status_code==404
    assert conn.scalar(select(func.count()).select_from(t.quiz_attempts))==before


def test_direct_key_replay_preserves_frozen_ranges_after_scope_and_date_change(api_client,progress_world):
    w=progress_world;unit=w.unit(1,12);a=w.concept(unit,1,4);w.concept(unit,9,12)
    key=str(uuid4());body={'subjectId':w.subject['id'],'unitId':unit['id']};w.conn.commit()
    path='/api/review/relearn'
    assert post(api_client,w.user,path,body).status_code==422
    first=post(api_client,w.user,path,body,key);assert first.status_code==200,first.text
    old=dict(w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.unit_id==unit['id'])).mappings().one())
    old_records=w.conn.execute(select(t.study_records)).all()
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=w.day+timedelta(days=1)))
    w.conn.execute(update(t.concepts).where(t.concepts.c.id==a['id']).values(end_page=2));w.conn.commit()
    assert post(api_client,w.user,path,body,key.upper().replace('-','')).json()==first.json()
    assert dict(w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.id==old['id'])).mappings().one())==old
    assert w.conn.execute(select(t.study_records)).all()==old_records
    w.conn.commit()
    second=post(api_client,w.user,path,body,str(uuid4()));assert second.status_code==200,second.text
    assert second.json()['data']['round']==2
    rows=w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.unit_id==unit['id']).order_by(t.learning_passes.c.pass_number)).mappings().all()
    assert [r['evidence_snapshot']['requiredRanges'] for r in rows]==[[[1,4],[9,12]],[[1,2],[9,12]]]


def test_direct_completion_drives_custom_quiz_only_with_actual_in_scope_pages(api_client,progress_world):
    w=progress_world;unit=w.unit(1,12);w.concept(unit,1,3);w.concept(unit,8,12)
    included={add_question(w,unit,page=p) for p in (1,3,8,12)}
    excluded=add_question(w,unit,page=5)
    w.conn.commit()
    done=post(api_client,w.user,'/api/review/relearn',{'subjectId':w.subject['id'],'unitId':unit['id']},str(uuid4()))
    assert done.status_code==200,done.text
    body={'subjectId':w.subject['id'],'scopes':[{'unitId':unit['id'],'from':1,'to':3},{'unitId':unit['id'],'from':8,'to':12}],
        'difficulty':'high','count':50,'weight':'even'}
    result=post(api_client,w.user,'/api/review/custom-quiz',body)
    assert result.status_code==200,result.text
    data=result.json()['data'];assert data['count']==4 and data['difficulty']=='low'
    actual=set(w.conn.scalars(select(t.quiz_items.c.question_id).where(t.quiz_items.c.quiz_session_id==data['quizId'])))
    assert actual==included and excluded not in actual
    w.conn.commit()
    body['scopes']=[{'unitId':unit['id'],'from':1,'to':12}]
    invalid=post(api_client,w.user,'/api/review/custom-quiz',body)
    assert invalid.status_code==422,invalid.text


@pytest.mark.parametrize('corruption',['foreign_material','uncompleted_page','foreign_concept'])
def test_custom_bank_never_crosses_actual_evidence_identity(api_client,progress_world,corruption):
    w=progress_world;unit=w.unit(1,10);other=w.unit(1,10)
    w.task((unit,1,10),done=[(unit,1,5)],status='partial')
    valid=add_question(w,unit,page=3);bad=add_question(w,unit,page=3)
    if corruption=='foreign_material':w.conn.execute(update(t.question_sources).where(t.question_sources.c.question_id==bad).values(material_id=other['material_id']))
    if corruption=='uncompleted_page':w.conn.execute(update(t.question_sources).where(t.question_sources.c.question_id==bad).values(end_page=6))
    if corruption=='foreign_concept':
        concept=w.concept(other,1,5)
        w.conn.execute(update(t.questions).where(t.questions.c.id==bad).values(concept_id=concept['id']))
    w.conn.commit()
    body={'subjectId':w.subject['id'],'scopes':[{'unitId':unit['id'],'from':1,'to':5}],
        'difficulty':'low','count':5,'weight':'weak'}
    result=post(api_client,w.user,'/api/review/custom-quiz',body);assert result.status_code==200,result.text
    selected=set(w.conn.scalars(select(t.quiz_items.c.question_id).where(t.quiz_items.c.quiz_session_id==result.json()['data']['quizId'])))
    assert selected=={valid}
