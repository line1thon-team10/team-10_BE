from copy import deepcopy
from dataclasses import replace
from uuid import UUID,uuid4
import pytest
from sqlalchemy import func,select,update
from app.ai.gateway import GatewayResult
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import lock_user
from tests.test_plan_draft import TODAY,NOW,plan_case
from tests.test_plan_addition_e2e import addition_case


@pytest.fixture
def voice_case(addition_case,monkeypatch):
    from app.services import schedule_voice
    from app.db.session import engine
    event={'kind':'meet','title':'친구 약속','days':None,'date':TODAY.isoformat(),'start':'15:00','end':'16:00'}
    class Model:
        calls=0
        callback=None
        failed=False
        value={'message':'일정을 확인해 주세요','chips':[],'isConfirmed':True,'goals':[],'event':event}
        def run(self,request,**_):
            self.calls+=1
            with engine.begin() as conn:
                assert lock_user(conn,UUID(request.owner_id)) is not None
            if self.callback:self.callback()
            if self.failed:raise ApiError('AI_FAILED','provider unavailable',503)
            return GatewayResult(deepcopy(self.value),str(uuid4()),'live',1)
    model=Model()
    monkeypatch.setattr(schedule_voice,'AiGateway',lambda *_args,**_kwargs:model)
    return {**addition_case,'headers':{'X-Anonymous-Id':addition_case['user']},'model':model,'text':f'{TODAY} 15:00부터 16:00까지 친구 약속'}


def preview(client,case,text=None):
    return client.post('/api/schedules/voice',headers=case['headers'],json={'text':case['text'] if text is None else text})


def apply(client,case,identifier):
    return client.post('/api/schedules/voice/apply',headers=case['headers'],json={'previewId':identifier})


def test_voice_is_only_preview_until_approved_and_approval_replays(api_client,domain_connection,voice_case):
    conn,case=domain_connection,voice_case
    events=conn.execute(select(t.calendar_events)).all();tasks=conn.execute(select(t.study_tasks)).all();conn.commit()
    candidate=preview(api_client,case);assert candidate.status_code==200,candidate.text
    value=candidate.json()['data'];assert value['event']['date']==TODAY.isoformat() and value['moves']==[]
    assert conn.execute(select(t.calendar_events)).all()==events
    assert conn.execute(select(t.study_tasks)).all()==tasks;conn.commit()
    response=apply(api_client,case,value['previewId']);assert response.status_code==200,response.text
    assert apply(api_client,case,value['previewId']).json()==response.json()
    assert conn.scalar(select(func.count()).select_from(t.calendar_events))==len(events)+1
    assert conn.execute(select(t.study_tasks)).all()==tasks
    assert case['model'].calls==1


@pytest.mark.parametrize('kind',['ambiguous','wrong_date','missing_end','weekday','provider'])
def test_voice_rejects_ungrounded_coordinates_without_domain_writes(api_client,domain_connection,voice_case,kind):
    conn,case=domain_connection,voice_case
    before=(conn.execute(select(t.calendar_events)).all(),conn.execute(select(t.study_tasks)).all());conn.commit()
    text=case['text']
    if kind=='ambiguous':text='토요일 저녁에 친구 약속'
    elif kind=='wrong_date':case['model'].value['event']['date']=(TODAY.replace(day=TODAY.day+1)).isoformat()
    elif kind=='missing_end':text=f'{TODAY} 15:00 친구 약속'
    elif kind=='weekday':text=case['text']+' 화요일'
    else:case['model'].failed=True
    response=preview(api_client,case,text)
    assert response.status_code==(503 if kind=='provider' else 400),response.text
    assert (conn.execute(select(t.calendar_events)).all(),conn.execute(select(t.study_tasks)).all())==before
    assert conn.scalar(select(func.count()).select_from(t.plan_proposals).where(t.plan_proposals.c.proposal_type=='schedule'))==0


def test_plan_change_during_interpretation_rejects_late_preview(api_client,domain_connection,voice_case):
    from app.db.session import engine
    conn,case=domain_connection,voice_case
    def change():
        with engine.begin() as other:
            other.execute(update(t.plans).where(t.plans.c.user_id==case['user']).values(revision=t.plans.c.revision+1))
    case['model'].callback=change
    response=preview(api_client,case)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE'
    assert conn.execute(select(t.calendar_events)).all()==[]


def test_failed_voice_approval_rolls_back_insert_and_can_retry(api_client,domain_connection,voice_case,monkeypatch):
    from app.services import schedule_voice
    conn,case=domain_connection,voice_case
    candidate=preview(api_client,case).json()['data']['previewId']
    original=schedule_voice.VOICE_DOMAIN
    def fail(*_):raise RuntimeError('injected response failure')
    monkeypatch.setattr(schedule_voice,'VOICE_DOMAIN',replace(original,after_apply=fail))
    assert apply(api_client,case,candidate).status_code==500
    assert conn.execute(select(t.calendar_events)).all()==[];conn.commit()
    monkeypatch.setattr(schedule_voice,'VOICE_DOMAIN',original)
    assert apply(api_client,case,candidate).status_code==200
    assert conn.scalar(select(func.count()).select_from(t.calendar_events))==1
