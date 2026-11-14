from datetime import timedelta
import hashlib
from uuid import uuid4
import pytest
from sqlalchemy import func,select,update
from app.db import schema as t
from app.services.learning_evidence import revalidate_record_passes
from tests.test_progress_contract import progress_world
from tests.test_remedial_tasks import remedial_case
from tests.test_mastery_rules import mastery_case


def request(client,w,unit,key,**changes):
    w.conn.commit()
    return client.post('/api/review/relearn',headers={'X-Anonymous-Id':w.user,**({'Idempotency-Key':key} if key is not None else {})},
        json={'subjectId':w.subject['id'],'unitId':unit['id'],**changes})


def test_direct_relearn_records_current_scope_without_plan_or_score_change(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10);w.concept(unit,1,3,sort_order=0);w.concept(unit,7,10,sort_order=1,code="C2");w.concept(unit,4,6,in_scope=False,sort_order=2,code="C3")
    before=(w.conn.execute(select(t.plans)).all(),w.conn.execute(select(t.units.c.mastery_score,t.units.c.mastery_level).where(t.units.c.id==unit['id'])).all())
    key=str(uuid4());response=request(api_client,w,unit,key)
    assert response.status_code==200,response.text
    data=response.json()['data'];assert data['round']==1 and data['relearnedOn']==w.day.isoformat()
    row=w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.unit_id==unit['id'])).mappings().one()
    snapshot=row['evidence_snapshot']
    assert snapshot['requiredRanges']==[[1,3],[7,10]] and snapshot['masteryScopeRevision']
    assert row['evidence_key']=='direct:'+hashlib.sha256(key.encode()).hexdigest()
    task=w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==row['source_task_id'])).mappings().one()
    assert task['plan_id'] is None and task['task_type']=='review'
    assert (w.conn.execute(select(t.plans)).all(),w.conn.execute(select(t.units.c.mastery_score,t.units.c.mastery_level).where(t.units.c.id==unit['id'])).all())==before
    w.conn.commit();assert request(api_client,w,unit,key.upper().replace('-','')).json()==response.json()
    assert request(api_client,w,unit,str(uuid4())).json()['error']['code']=='ALREADY'
    other=w.unit(1,5)
    assert request(api_client,w,other,key).json()['error']['code']=='IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('bad',['missing_key','invalid_key','foreign','retired','excluded','time'])
def test_invalid_relearn_is_atomic(api_client,progress_world,bad):
    w=progress_world;unit=w.unit(1,10);key=str(uuid4())
    if bad=='missing_key':key=None
    if bad=='invalid_key':key='invalid'
    if bad=='foreign':unit={**unit,'id':str(uuid4())}
    if bad=='retired':w.conn.execute(update(t.units).where(t.units.c.id==unit['id']).values(retired_at=w.now))
    if bad=='excluded':w.conn.execute(update(t.units).where(t.units.c.id==unit['id']).values(in_scope=False))
    if bad=='time':w.conn.execute(update(t.units).where(t.units.c.id==unit['id']).values(quantity_basis='time',start_page=None,end_page=None,estimated_minutes=30))
    before=w.conn.scalar(select(func.count()).select_from(t.study_tasks))
    response=request(api_client,w,unit,key)
    assert response.status_code==(404 if bad in ('foreign','retired') else 422),response.text
    if bad in ('foreign','retired'):assert response.json()['error']['code']=='UNIT_NOT_FOUND'
    assert w.conn.scalar(select(func.count()).select_from(t.study_tasks))==before


def test_invalidated_direct_pass_can_restore_without_new_round_or_completion_date(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10);key=str(uuid4())
    assert request(api_client,w,unit,key).status_code==200
    row=dict(w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.unit_id==unit['id'])).mappings().one())
    record=row['evidence_snapshot']['recordIds'][0]
    w.conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==record).values(end_page=5))
    revalidate_record_passes(w.conn,w.user,record,w.now)
    assert w.conn.scalar(select(t.learning_passes.c.invalidated_at).where(t.learning_passes.c.id==row['id'])) is not None
    w.conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==record).values(end_page=10))
    revalidate_record_passes(w.conn,w.user,record,w.now+timedelta(days=1))
    actual=dict(w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.id==row['id'])).mappings().one())
    assert actual==row


def test_failed_receipt_rolls_back_direct_completion(api_client,progress_world,monkeypatch):
    from app.repositories.record_requests import RecordRequestRepository
    w=progress_world;unit=w.unit(1,5);key=str(uuid4())
    before=w.conn.scalar(select(func.count()).select_from(t.study_tasks))
    original=RecordRequestRepository.save
    monkeypatch.setattr(RecordRequestRepository,'save',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('receipt failed')))
    assert request(api_client,w,unit,key).status_code==500
    assert w.conn.scalar(select(func.count()).select_from(t.study_tasks))==before
    assert w.conn.scalar(select(func.count()).select_from(t.learning_passes).where(t.learning_passes.c.unit_id==unit['id']))==0
    monkeypatch.setattr(RecordRequestRepository,'save',original)
    assert request(api_client,w,unit,key).status_code==200


def test_direct_completion_supplies_custom_scope_without_daily_plan(api_client,progress_world):
    from tests.test_quiz_selection import add_question
    from tests.test_custom_quiz import create,scope
    w=progress_world;unit=w.unit(1,5);add_question(w,unit)
    before=create(api_client,w,[scope(unit,1,5)],count=1)
    assert before.status_code==422
    assert request(api_client,w,unit,str(uuid4())).status_code==200
    custom=create(api_client,w,[scope(unit,1,5)],count=1)
    assert custom.status_code==200,custom.text
    assert custom.json()['data']['count']==1
    w.conn.commit()
    today=api_client.get('/api/today',headers={'X-Anonymous-Id':w.user})
    assert today.status_code==200,today.text
    assert today.json()['data']['tasks']==[] and today.json()['data']['progress']['totalPages']==0


def test_books_task_id_supports_direct_completion_correction_and_restoration(api_client,progress_world):
    w=progress_world;unit=w.unit(1,5);key=str(uuid4())
    first=request(api_client,w,unit,key)
    assert first.status_code==200,first.text
    headers={'X-Anonymous-Id':w.user}
    books=api_client.get('/api/books',headers=headers).json()['data']
    subject=next(s for s in books['subjects'] if s['subjectId']==w.subject['id'])
    shown=next(u for u in subject['units'] if u['id']==unit['id'])
    assert shown['round']==1 and len(shown['blocks'])==1
    task=shown['blocks'][0]['task']['id']
    before=dict(w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.unit_id==unit['id'])).mappings().one())
    w.conn.commit()
    partial=api_client.patch(f'/api/tasks/{task}/record',headers=headers,json={'toPage':3})
    assert partial.status_code==200,partial.text
    invalid=dict(w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.id==before['id'])).mappings().one())
    assert invalid['invalidated_at'] is not None
    assert all(invalid[key]==before[key] for key in before if key!='invalidated_at')
    w.conn.commit()
    assert request(api_client,w,unit,key).json()==first.json()
    restored=api_client.patch(f'/api/tasks/{task}/record',headers=headers,json={'toPage':5})
    assert restored.status_code==200,restored.text
    assert dict(w.conn.execute(select(t.learning_passes).where(t.learning_passes.c.id==before['id'])).mappings().one())==before
    w.conn.commit()
    today=api_client.get('/api/today',headers=headers).json()['data']
    assert today['tasks']==[] and today['progress']['totalPages']==0


@pytest.mark.parametrize('receipt_fails',[False,True])
def test_direct_first_pass_rechecks_weak_result_atomically(api_client,domain_connection,remedial_case,monkeypatch,receipt_fails):
    from tests.test_remedial_tasks import submit
    from app.repositories.record_requests import RecordRequestRepository
    from app.core.responses import ApiError
    conn,c=domain_connection,remedial_case
    conn.execute(t.learning_passes.delete().where(t.learning_passes.c.id==c['initial']))
    conn.commit()
    submit(api_client,conn,c)
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==0
    conn.commit()
    if receipt_fails:
        def fail(*args,**kwargs):raise ApiError('INVALID_INPUT','receipt failed',422)
        monkeypatch.setattr(RecordRequestRepository,'save',fail)
    headers={**c['headers'],'Idempotency-Key':str(uuid4())}
    body={'subjectId':c['subject_id'],'unitId':c['units'][0]['id']}
    response=api_client.post('/api/review/relearn',headers=headers,json=body)
    assert response.status_code==(422 if receipt_fails else 200),response.text
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==(0 if receipt_fails else 1)
    passes=list(conn.execute(select(t.learning_passes).where(t.learning_passes.c.unit_id==body['unitId'])).mappings())
    assert len(passes)==(0 if receipt_fails else 1)
    if receipt_fails:
        assert conn.scalar(select(func.count()).select_from(t.study_tasks).where(t.study_tasks.c.task_type.in_(('review','remedial'))))==0
    else:
        assert passes[0]['completion_kind']=='full_relearn'
        obligation=conn.execute(select(t.remedial_obligations)).mappings().one()
        assert obligation['required_ranges']==[[1,3],[8,10]]
        conn.commit()
        assert api_client.post('/api/review/relearn',headers=headers,json=body).json()==response.json()
        assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==1
