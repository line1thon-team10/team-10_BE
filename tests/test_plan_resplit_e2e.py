"""Request identity, single follow-up and initial-versus-approved plan semantics."""
from uuid import uuid4
import pytest
from sqlalchemy import select,update
from app.db import schema as t
from app.services.plan_resplit import PlanResplitService
from tests.test_plan_draft import NOW,TODAY,plan_case
from tests.test_plan_addition_e2e import addition_case


def state(conn):
    return {table.name:conn.execute(select(table).order_by(*table.primary_key.columns)).all()
        for table in (t.plans,t.subjects,t.units,t.study_tasks,t.study_task_ranges,t.study_records,t.study_record_ranges,t.study_sessions,t.learning_passes)}


def call(client,case,method,path,**kwargs):
    return getattr(client,method)(path,headers={'X-Anonymous-Id':case['user']},**kwargs)


@pytest.fixture
def interpretation(monkeypatch):
    calls=[]
    def interpret(self,user,payload,saved):
        calls.append((user,payload,saved))
        if '모호' in payload['request'] and payload['answer'] is None:
            return {'ready':False,'question':{'text':'공부할 날짜를 알려 주세요','choices':['내일부터']},'windows':[]}
        return {'ready':True,'question':None,'windows':[{'subjectId':None,'dates':['2026-11-17','2026-11-18'],'minutesPerDay':None}]}
    monkeypatch.setattr(PlanResplitService,'_interpret',interpret)
    return calls


def prepare(client,case,**body):
    return call(client,case,'post','/api/plan/resplit',json={'request':'내일부터 다시 배분',**body})


def test_active_resplit_compares_real_minutes_then_replays_approval(api_client,domain_connection,addition_case,interpretation):
    conn,case=domain_connection,addition_case
    before=state(conn);conn.commit()
    response=prepare(api_client,case);assert response.status_code==200,response.text
    identifier=response.json()['data']['previewId']
    assert state(conn)==before;conn.commit()
    value=call(api_client,case,'get','/api/plan/resplit',params={'previewId':identifier}).json()['data']
    assert all(type(row['before']) is type(row['after']) is int for row in value['days'])
    assert sum(row['before'] for row in value['days'])==sum(row['after'] for row in value['days'])==case['old_task']['estimated_minutes']
    assert prepare(api_client,case).json()==response.json() and len(interpretation)==1
    applied=call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier})
    assert applied.status_code==200,applied.text
    after=state(conn);conn.commit()
    assert call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier}).json()==applied.json()
    assert state(conn)==after


def test_initial_apply_only_replaces_draft_until_explicit_plan_approval(api_client,domain_connection,plan_case,interpretation):
    conn,case=domain_connection,plan_case
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['active']).values(status='archived'));conn.commit()
    before=state(conn);conn.commit()
    response=prepare(api_client,case);assert response.status_code==200,response.text
    identifier=response.json()['data']['previewId']
    response=call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier})
    assert response.status_code==200,response.text
    assert state(conn)==before;conn.commit()
    new=call(api_client,case,'get','/api/plan/draft')
    assert new.status_code==200,new.text
    assert new.json()['data']['firstWeek'][0]['minutes']=={}
    approved=call(api_client,case,'post','/api/plan/approve',json={'previewId':identifier})
    assert approved.status_code==200 and approved.json()['data']=={'approved':True},approved.text
    assert conn.scalar(select(t.plans.c.revision))==1
    dates=conn.scalars(select(t.study_tasks.c.scheduled_date)).all()
    assert dates and all(day>TODAY for day in dates)
    after=state(conn);conn.commit()
    assert call(api_client,case,'post','/api/plan/approve',json={'previewId':identifier}).json()==approved.json()
    assert state(conn)==after


def test_followup_is_owned_exactly_once_and_needs_matching_request(api_client,domain_connection,addition_case,interpretation):
    case=addition_case
    response=prepare(api_client,case,request='모호한 요청');assert response.status_code==200,response.text
    data=response.json()['data'];assert data['ready'] is False
    request_id=data['requestId']
    for extra in [{'request':'바꾼 요청','requestId':request_id,'answer':'내일부터'},
                  {'requestId':str(uuid4()),'answer':'내일부터'}, {'answer':'내일부터'}]:
        assert prepare(api_client,case,**extra).status_code==422
    response=prepare(api_client,{**case,'user':case['other_user']},request='모호한 요청',requestId=request_id,answer='내일부터')
    assert response.status_code in (400,422),response.text
    ready=prepare(api_client,case,request='모호한 요청',requestId=request_id,answer='내일부터')
    assert ready.status_code==200 and ready.json()['data']['ready'],ready.text
    assert ready.json()['data']['requestId']==request_id and len(interpretation)==2
    assert prepare(api_client,case,request='모호한 요청',requestId=request_id,answer='내일부터').json()==ready.json()
    assert prepare(api_client,case,request='모호한 요청',requestId=request_id,answer='다른 답').status_code==422


def test_second_question_is_failure_and_does_not_change_plan(api_client,domain_connection,addition_case,interpretation,monkeypatch):
    case,conn=addition_case,domain_connection
    response=prepare(api_client,case,request='모호한 요청').json()['data']
    before=state(conn);conn.commit()
    monkeypatch.setattr(PlanResplitService,'_interpret',lambda *args:{'ready':False,'question':{'text':'다시 질문','choices':[]},'windows':[]})
    result=prepare(api_client,case,request='모호한 요청',requestId=response['requestId'],answer='잘 모르겠어요')
    assert result.status_code==503 and result.json()['error']['code']=='AI_FAILED'
    assert state(conn)==before


def test_stale_and_cancelled_candidates_cannot_apply(api_client,domain_connection,addition_case,interpretation):
    conn,case=domain_connection,addition_case
    response=prepare(api_client,case);assert response.status_code==200,response.text
    identifier=response.json()['data']['previewId']
    conn.execute(update(t.materials).where(t.materials.c.id==case['material']).values(analysis_version='next'));conn.commit()
    before=state(conn);conn.commit()
    assert call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier}).status_code==409
    assert call(api_client,case,'delete','/api/plan/resplit',params={'previewId':identifier}).json()['data']=={'cancelled':True}
    assert call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier}).status_code==409
    assert state(conn)==before


def test_ambiguous_get_requires_preview_id_and_blank_text_is_empty(api_client,addition_case,interpretation):
    case=addition_case
    assert prepare(api_client,case,request=' ').status_code==400
    assert prepare(api_client,case).status_code==200
    assert prepare(api_client,case,request='내일부터 다시 나눠줘').status_code==200
    response=call(api_client,case,'get','/api/plan/resplit')
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_REQUIRED'
    assert call(api_client,case,'delete','/api/plan/resplit').status_code==422


def test_concurrent_identical_requests_publish_only_one_ready_candidate(api_client,domain_connection,addition_case,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event
    gate=Barrier(2)
    entered=Event()
    def interpret(*args):
        entered.set()
        gate.wait(timeout=10)
        return {'ready':True,'question':None,'windows':[]}
    monkeypatch.setattr(PlanResplitService,'_interpret',interpret)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(prepare,api_client,addition_case)]
        assert entered.wait(timeout=10)
        futures.append(executor.submit(prepare,api_client,addition_case))
        responses=[future.result(timeout=20) for future in futures]
    assert all(response.status_code in (200,409) for response in responses),[response.text for response in responses]
    successes=[response.json()['data'] for response in responses if response.status_code==200]
    assert successes and len({value['previewId'] for value in successes})==1
    ready=domain_connection.scalars(select(t.plan_proposal_states.c.proposal_id).where(
        t.plan_proposal_states.c.kind=='remaining_plan_resplit',t.plan_proposal_states.c.status=='ready')).all()
    assert ready==[successes[0]['previewId']]


@pytest.mark.parametrize('stale',[False,True])
def test_initial_resplit_preserves_latest_explicit_subject_selection(api_client,domain_connection,plan_case,interpretation,stale):
    conn,case=domain_connection,plan_case
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['active']).values(status='archived'))
    other,material,unit=(str(uuid4()) for _ in range(3))
    subject=dict(conn.execute(select(t.subjects).where(t.subjects.c.id==case['subject'])).mappings().one())
    conn.execute(t.subjects.insert().values(**{**subject,'id':other,'code':other,'name':'미선택 과목'}))
    row=dict(conn.execute(select(t.materials).where(t.materials.c.id==case['material'])).mappings().one())
    conn.execute(t.materials.insert().values(**{**row,'id':material,'subject_id':other,'sha256':'unselected'}))
    row=dict(conn.execute(select(t.units).where(t.units.c.id==case['unit'])).mappings().one())
    conn.execute(t.units.insert().values(**{**row,'id':unit,'material_id':material}));conn.commit()
    assert call(api_client,case,'get','/api/plan/draft').status_code==200
    selected=call(api_client,case,'get','/api/plan/draft',params={'draftSubjectIds':case['subject']})
    assert selected.status_code==200,selected.text
    if stale:
        conn.execute(update(t.materials).where(t.materials.c.id==case['material']).values(analysis_version='changed'))
        conn.commit()
    response=prepare(api_client,case);assert response.status_code==200,response.text
    identifier=response.json()['data']['previewId']
    assert call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier}).status_code==200
    current=call(api_client,case,'get','/api/plan/draft')
    assert current.status_code==200,current.text
    assert {row['subjectId'] for row in current.json()['data']['allocation']}=={case['subject']}
    response=call(api_client,case,'post','/api/plan/approve',json={'previewId':identifier})
    assert response.status_code==200,response.text
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==other))=='draft'
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==case['subject']))=='active'
    assert set(conn.scalars(select(t.study_tasks.c.subject_id)))=={case['subject']}
