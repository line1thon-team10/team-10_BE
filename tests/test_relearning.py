from datetime import timedelta
import hashlib
from uuid import uuid4
import pytest
from sqlalchemy import func,select,update
from app.db import schema as t
from app.services.learning_evidence import revalidate_record_passes
from tests.test_progress_contract import progress_world


def request(client,w,unit,key,**changes):
    w.conn.commit()
    return client.post('/api/review/relearn',headers={'X-Anonymous-Id':w.user,**({'Idempotency-Key':key} if key is not None else {})},
        json={'subjectId':w.subject['id'],'unitId':unit['id'],**changes})


def test_direct_relearn_records_current_scope_without_plan_or_score_change(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10);w.concept(unit,1,3);w.concept(unit,7,10);w.concept(unit,4,6,in_scope=False)
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
