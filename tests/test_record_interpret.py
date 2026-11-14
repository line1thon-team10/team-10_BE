"""Real HTTP/database E2E with only the external model boundary replaced."""
from uuid import UUID, uuid4
import json

import pytest
from sqlalchemy import select, update

from app.ai.gateway import GatewayResult
from app.ai.record_interpret_adapter import explicit_ranges
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import lock_user
from tests.test_progress_contract import progress_world


def snapshot(conn):
    return tuple(conn.execute(select(table)).all() for table in
        (t.study_tasks,t.study_task_ranges,t.study_records,t.study_record_ranges,t.learning_passes,t.study_blocks))


@pytest.fixture
def interpretation_case(api_client,progress_world,monkeypatch):
    from app.services import record_interpret
    from app.db.session import engine
    w=progress_world
    unit=w.unit(10,20)
    w.conn.execute(update(t.materials).where(t.materials.c.id==unit['material_id']).values(original_name='교재A.pdf'))
    w.conn.execute(update(t.units).where(t.units.c.id==unit['id']).values(title='A단원'))
    task=w.task((unit,10,20))
    w.conn.commit()
    class Model:
        calls=0
        transform=None
        callback=None
        fail=False
        def run(self,request,**kwargs):
            self.calls+=1
            assert request.owner_id==w.user
            context=json.loads(request.identity.validation_json)
            assert request.identity.materials[0].material_id==unit['material_id'] or len(request.identity.materials)>1
            # The request connection and capture lock are released during AI.
            with engine.begin() as conn:
                assert lock_user(conn,UUID(w.user)) is not None
            if self.callback:
                self.callback(engine)
            if self.fail:
                raise ApiError('AI_FAILED','외부 모델 실패',503)
            value={'completedRanges':explicit_ranges(context['text'],context['ranges']), 'isConfirmed':True}
            if self.transform:
                value=self.transform(value,context)
            return GatewayResult(value,'','live',1)
    model=Model()
    monkeypatch.setattr(record_interpret,'AiGateway',lambda *_:model)
    return {'world':w,'unit':unit,'task':task,'model':model}


def interpret(client,case,text,**extra):
    return client.post('/api/records/interpret',headers={'X-Anonymous-Id':case['world'].user},
        json={'taskId':case['task'],'text':text,**extra})


@pytest.mark.parametrize('text,end',[('13쪽까지 공부했어',13),('10~15쪽 읽었어',15),
    ('10페이지부터 18페이지까지 봤어',18),('p.10~p.15까지 읽었어',15),('p.17까지 읽었어',17),('전체 다 공부했어',20)])
def test_explicit_page_candidate_never_saves_a_record(api_client,interpretation_case,text,end):
    case=interpretation_case;w=case['world']
    before=snapshot(w.conn);w.conn.commit()
    response=interpret(api_client,case,text)
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert (data['from'],data['to'])==(10,end)
    row=w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==case['task'])).mappings().one()
    assert data['completedRanges']==[{'rangeId':row['id'],'toPage':end}]
    assert f'10~{end}쪽' in data['label']
    assert snapshot(w.conn)==before and case['model'].calls==1


@pytest.mark.parametrize('text',['아직 못 했어','공부한 것이 없어','0쪽 공부했어'])
def test_explicit_zero_does_not_erase_existing_record(api_client,interpretation_case,text):
    case=interpretation_case;w=case['world']
    before=snapshot(w.conn);w.conn.commit()
    response=interpret(api_client,case,text)
    assert response.status_code==200,response.text
    assert response.json()['data']=={'from':None,'to':None,'completedRanges':[],'label':'완료한 페이지 없음'}
    assert snapshot(w.conn)==before


@pytest.mark.parametrize('text',['절반 정도 공부했어','아마 13쪽까지 읽었어','13쪽까지 읽을 예정',
    '13쪽까지 읽었어?','13쪽까지 읽었다고 치면','13쪽까지 읽은 게 아니야','5쪽 읽었어',
    '21쪽까지 읽었어','9쪽까지 읽었어','12~15쪽 읽었어','p.12~p.15까지 읽었어','18~12쪽 읽었어',
    '다 못 했어','다 읽지는 못했어','교재A 못했어, 교재B는 완료했어','없는자료.pdf 13쪽까지 읽었어','13.5쪽까지 읽었어','-13쪽까지 읽었어',
    '13쪽까지 읽었고, 15쪽은 읽을 예정'])
def test_ambiguous_or_out_of_range_text_is_parse_failed_before_ai(api_client,interpretation_case,text):
    case=interpretation_case;w=case['world']
    before=snapshot(w.conn);w.conn.commit()
    response=interpret(api_client,case,text)
    assert response.status_code==400,response.text
    assert response.json()['error']['code']=='PARSE_FAILED'
    assert case['model'].calls==0 and snapshot(w.conn)==before


def test_multiple_pdf_identity_is_required_and_both_prefixes_survive(api_client,interpretation_case):
    case=interpretation_case;w=case['world'];a=case['unit'];b=w.unit(10,20)
    w.conn.execute(update(t.materials).where(t.materials.c.id==b['material_id']).values(original_name='교재B.pdf'))
    w.conn.execute(update(t.units).where(t.units.c.id==b['id']).values(title='B단원'))
    case['task']=w.task((a,10,20),(b,10,20))
    w.conn.commit()
    response=interpret(api_client,case,'13쪽까지 읽었어')
    assert response.status_code==400 and case['model'].calls==0
    before=snapshot(w.conn);w.conn.commit()
    response=interpret(api_client,case,'교재A.pdf 13쪽까지, 교재B.pdf 17쪽까지 읽었어')
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['from'] is None and data['to'] is None
    rows=w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==case['task']).order_by(t.study_task_ranges.c.sort_order)).mappings().all()
    assert data['completedRanges']==[{'rangeId':rows[0]['id'],'toPage':13},{'rangeId':rows[1]['id'],'toPage':17}]
    assert snapshot(w.conn)==before


@pytest.mark.parametrize('kind',['wrong_end','foreign_id','duplicate','extra','boolean','missing'])
def test_provider_cannot_fabricate_or_drop_explicit_pages(api_client,interpretation_case,kind):
    case=interpretation_case;w=case['world']
    def transform(value,context):
        if kind=='wrong_end': value['completedRanges'][0]['toPage']=14
        if kind=='foreign_id': value['completedRanges'][0]['rangeId']=str(uuid4())
        if kind=='duplicate': value['completedRanges']*=2
        if kind=='extra': value['label']='공부 끝'
        if kind=='boolean': value['completedRanges'][0]['toPage']=True
        if kind=='missing': value['completedRanges']=[]
        return value
    case['model'].transform=transform
    before=snapshot(w.conn);w.conn.commit()
    response=interpret(api_client,case,'13쪽까지 읽었어')
    assert response.status_code==503 and response.json()['error']['code']=='AI_FAILED',response.text
    assert snapshot(w.conn)==before


@pytest.mark.parametrize('change',['task','material','day','record'])
def test_changes_during_ai_invalidate_candidate(api_client,interpretation_case,change):
    case=interpretation_case;w=case['world']
    def callback(engine):
        with engine.begin() as conn:
            if change=='task': conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==case['task']).values(title='changed'))
            elif change=='material': conn.execute(update(t.materials).where(t.materials.c.id==case['unit']['material_id']).values(analysis_version='new'))
            elif change=='day': conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=w.day.replace(day=10)))
            else: conn.execute(t.study_records.insert().values(id=str(uuid4()),task_id=case['task'],result='none',study_date=w.day,recorded_at=w.now,updated_at=w.now))
    case['model'].callback=callback
    response=interpret(api_client,case,'13쪽까지 읽었어')
    assert response.status_code==409 and response.json()['error']['code']=='INPUT_STALE',response.text


def test_foreign_task_and_missing_auth_are_rejected_before_ai(api_client,interpretation_case):
    case=interpretation_case
    other=api_client.post('/api/session').json()['data']['anonymousId']
    response=api_client.post('/api/records/interpret',headers={'X-Anonymous-Id':other},json={'taskId':case['task'],'text':'13쪽까지 읽었어'})
    assert response.status_code==404,response.text
    assert api_client.post('/api/records/interpret',json={'taskId':case['task'],'text':'13쪽까지 읽었어'}).status_code==401
    assert case['model'].calls==0


def test_provider_failure_never_changes_records(api_client,interpretation_case):
    case=interpretation_case;w=case['world'];case['model'].fail=True
    before=snapshot(w.conn);w.conn.commit()
    response=interpret(api_client,case,'13쪽까지 읽었어')
    assert response.status_code==503
    assert snapshot(w.conn)==before


@pytest.mark.parametrize('extra',[{'minutes':30},{'result':'done'},{'completedRanges':[]}])
def test_interpretation_cannot_smuggle_record_fields(api_client,interpretation_case,extra):
    response=interpret(api_client,interpretation_case,'13쪽까지 읽었어',**extra)
    assert response.status_code==422
    assert interpretation_case['model'].calls==0


def test_named_full_completion_cannot_complete_another_pdf(api_client,interpretation_case):
    case=interpretation_case;w=case['world'];a=case['unit'];b=w.unit(10,20)
    w.conn.execute(update(t.materials).where(t.materials.c.id==b['material_id']).values(original_name='교재B.pdf'))
    w.conn.execute(update(t.units).where(t.units.c.id==b['id']).values(title='B단원'))
    case['task']=w.task((a,10,20),(b,10,20));w.conn.commit()
    response=interpret(api_client,case,'교재A.pdf 다 읽었어')
    assert response.status_code==200,response.text
    values=response.json()['data']['completedRanges']
    first=w.conn.scalar(select(t.study_task_ranges.c.id).where(t.study_task_ranges.c.task_id==case['task'],t.study_task_ranges.c.unit_id==a['id']))
    assert values==[{'rangeId':first,'toPage':20}]
    w.conn.commit()
    assert interpret(api_client,case,'없는교재.pdf 다 읽었어').status_code==400


@pytest.mark.parametrize('text',['','   ','\x00','\ud800'])
def test_empty_or_invalid_text_is_rejected_before_ai(api_client,interpretation_case,text):
    case=interpretation_case
    if '\ud800' in text:
        # Send escaped JSON so the server's text validator handles the surrogate.
        response=api_client.post('/api/records/interpret',headers={'X-Anonymous-Id':case['world'].user,'Content-Type':'application/json'},
            content=json.dumps({'taskId':case['task'],'text':text}))
    else:
        response=interpret(api_client,case,text)
    assert response.status_code in (400,422),response.text
    assert case['model'].calls==0


def test_unconfigured_real_gateway_returns_failure_without_record_writes(api_client,interpretation_case,monkeypatch):
    from app.ai.gateway import AiGateway
    from app.services import record_interpret
    case=interpretation_case;w=case['world']
    monkeypatch.setenv('GROQ_API_KEY','')
    monkeypatch.setattr(record_interpret,'AiGateway',AiGateway)
    before=snapshot(w.conn);w.conn.commit()
    response=interpret(api_client,case,'13쪽까지 읽었어')
    assert response.status_code==503 and response.json()['error']['code']=='AI_FAILED',response.text
    assert snapshot(w.conn)==before


def test_compound_page_claim_cannot_be_silently_omitted(api_client,interpretation_case):
    case=interpretation_case
    response=interpret(api_client,case,'13쪽까지 읽었어. 20쪽 끝냈어')
    assert response.status_code==400,response.text
    assert response.json()['error']['code']=='PARSE_FAILED'
    assert case['model'].calls==0


def test_model_uncertainty_returns_parse_failed_without_saving(api_client,interpretation_case):
    case=interpretation_case;w=case['world']
    case['model'].transform=lambda value,context:{'completedRanges':[], 'isConfirmed':False}
    before=snapshot(w.conn);w.conn.commit()
    response=interpret(api_client,case,'13쪽까지 읽었어')
    assert response.status_code==400,response.text
    assert response.json()['error']['code']=='PARSE_FAILED'
    assert snapshot(w.conn)==before


def test_similar_pdf_names_do_not_shadow_the_exact_filename(api_client,interpretation_case):
    case=interpretation_case;w=case['world'];a=case['unit'];b=w.unit(10,20)
    w.conn.execute(update(t.materials).where(t.materials.c.id==a['material_id']).values(original_name='book.pdf'))
    w.conn.execute(update(t.materials).where(t.materials.c.id==b['material_id']).values(original_name='book2.pdf'))
    case['task']=w.task((a,10,20),(b,10,20));w.conn.commit()
    response=interpret(api_client,case,'book2.pdf 13쪽까지 읽었어')
    assert response.status_code==200,response.text
    second=w.conn.scalar(select(t.study_task_ranges.c.id).where(t.study_task_ranges.c.task_id==case['task'],t.study_task_ranges.c.unit_id==b['id']))
    assert response.json()['data']['completedRanges']==[{'rangeId':second,'toPage':13}]
