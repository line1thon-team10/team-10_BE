from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_progress_contract import progress_world


def at_local(monkeypatch, hour, minute, second=0):
    from app.routers import today
    at = datetime(2026,11,9,hour-9,minute,second,tzinfo=timezone.utc)
    monkeypatch.setattr(today,'now_utc',lambda:at)


def read(client, world):
    return client.get('/api/today',headers={'X-Anonymous-Id':world.user})


def test_late_get_emits_exactly_once_at_2330_without_extra_record(api_client,progress_world,monkeypatch):
    w=progress_world
    unit=w.unit(1,100)
    w.task((unit,1,70),done=[(unit,1,70)],status='completed')
    w.task((unit,71,100))
    w.conn.commit()
    at_local(monkeypatch,23,29,59)
    assert read(api_client,w).json()['data']['event'] is None
    at_local(monkeypatch,23,30)
    first=read(api_client,w)
    assert first.status_code == 200,first.text
    event=first.json()['data']['event']
    assert event['reason']=='dayClose' and event['rate']==70
    assert event['recordedCount']==1 and event['totalCount']==2 and event['opened']==[]
    assert read(api_client,w).json()['data']['event'] is None
    assert len(w.conn.execute(select(t.user_notices).where(t.user_notices.c.user_id==w.user,
        t.user_notices.c.notice_type=='day_summary')).all())==1


@pytest.mark.parametrize('done,emitted',[(6999,False),(7000,True)])
def test_all_recorded_uses_unrounded_ratio(api_client,progress_world,monkeypatch,done,emitted):
    w=progress_world
    unit=w.unit(1,10000)
    w.task((unit,1,10000),done=[(unit,1,done)],status='partial')
    w.conn.commit()
    at_local(monkeypatch,12,0)
    data=read(api_client,w).json()['data']
    assert data['progress']['rate']==70
    assert (data['event'] is not None) == emitted


def test_post_then_get_share_notice_and_key_replays_first_event(api_client,progress_world,monkeypatch):
    w=progress_world
    unit=w.unit(1,100)
    task=w.task((unit,1,100))
    w.conn.commit()
    at_local(monkeypatch,12,0)
    headers={'X-Anonymous-Id':w.user,'Idempotency-Key':str(uuid4())}
    first=api_client.post('/api/records',headers=headers,json={'taskId':task,'result':'done'})
    assert first.status_code==200,first.text
    assert first.json()['data']['event']['reason']=='dayClose'
    assert read(api_client,w).json()['data']['event'] is None
    repeated=api_client.post('/api/records',headers=headers,json={'taskId':task,'result':'done'})
    assert repeated.json()==first.json()
    assert len(w.conn.execute(select(t.user_notices).where(t.user_notices.c.user_id==w.user,
        t.user_notices.c.notice_type=='day_summary')).all())==1


def test_quizless_progress_never_claims_quiz_open(api_client,progress_world,monkeypatch):
    w=progress_world
    quizless=w.unit(1,70,quiz_enabled=False)
    quiz=w.unit(1,30,quiz_enabled=True)
    task=w.task((quizless,1,70))
    w.task((quiz,1,30))
    w.conn.commit()
    at_local(monkeypatch,12,0)
    response=api_client.post('/api/records',headers={'X-Anonymous-Id':w.user},json={'taskId':task,'result':'done'})
    assert response.status_code==200,response.text
    event=response.json()['data']['event']
    assert event['type']=='toast' and event['reason']=='record' and event['rate']==70
    assert '열렸' not in event['detail']
    assert read(api_client,w).json()['data']['quiz']['open'] is False


def test_empty_day_never_emits_even_late(api_client,progress_world,monkeypatch):
    w=progress_world
    w.conn.commit()
    at_local(monkeypatch,23,59)
    assert read(api_client,w).json()['data']['event'] is None
