"""P2 actual allocations through HTTP, with durable approval and input freshness."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update, func

from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case


@pytest.fixture
def recovery_case(api_client,domain_connection,plan_case):
    case,conn=plan_case,domain_connection
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['active']).values(status='archived'))
    conn.commit()
    headers={'X-Anonymous-Id':case['user']}
    preview=api_client.get('/api/plan/draft',headers=headers).json()['data']['previewId']
    assert api_client.post('/api/plan/approve',headers=headers,json={'previewId':preview}).status_code==200
    plan=conn.execute(select(t.plans).where(t.plans.c.user_id==case['user'])).mappings().one()
    tasks=list(conn.execute(select(t.study_tasks).where(t.study_tasks.c.plan_id==plan['id'])).mappings())
    rid=str(uuid4())
    conn.execute(t.plan_change_requests.insert().values(id=rid,user_id=case['user'],base_plan_id=plan['id'],base_plan_revision=plan['revision'],
        kind='recovery',status='ready',input_payload={'schemaVersion':'1'},created_at=NOW,updated_at=NOW,
        recovery_context={'assessedDate':(TODAY-timedelta(days=1)).isoformat(),'tasks':[{'taskId':task['id'],'ranges':[]} for task in tasks],
            'subjects':[{'subjectId':case['subject'],'overduePages':20,'dailyAveragePages':10,'thresholdPages':5}]}))
    conn.commit()
    return {**case,'headers':headers,'request':rid,'tasks':tasks}


def options(client,case):
    return client.get('/api/plan/replan/options',headers=case['headers'])


def apply(client,case,preview,key='A'):
    return client.post('/api/plan/replan',headers=case['headers'],json={'previewId':preview,'key':key})


def test_actual_three_candidates_and_atomic_saved_approval(api_client,domain_connection,recovery_case):
    case,conn=recovery_case,domain_connection
    result=options(api_client,case)
    assert result.status_code==200,result.text
    value=result.json()['data']
    assert [item['key'] for item in value['options']]==['A','B','C']
    assert all(type(item['enabled']) is bool for item in value['options'])
    assert options(api_client,case).json()==result.json()
    changes=conn.scalar(select(t.plan_proposals.c.changes).where(t.plan_proposals.c.id==value['previewId']))
    for item in value['options']:
        placements=changes['options'][item['key']]['allocation']['placements']
        assert item['totalMinutes']==sum(task['minutes'] for task in placements)
        assert all(15<=task['minutes']<=60 for task in placements)
    conn.commit()
    first=apply(api_client,case,value['previewId'])
    assert first.status_code==200,first.text
    assert first.json()['data']['event']['planKey']=='A'
    assert apply(api_client,case,value['previewId']).json()==first.json()
    assert apply(api_client,case,value['previewId'],'C').status_code==409
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id==case['user']))==2
    assert conn.scalar(select(func.count()).select_from(t.user_notices).where(t.user_notices.c.user_id==case['user'],t.user_notices.c.notice_type=='recovery'))==1
    assert conn.scalar(select(t.plan_change_requests.c.status).where(t.plan_change_requests.c.id==case['request']))=='accepted'


def test_candidate_stales_on_learning_or_calendar_change(api_client,domain_connection,recovery_case):
    case,conn=recovery_case,domain_connection
    value=options(api_client,case).json()['data']
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['subject']).values(target_score=80))
    conn.commit()
    response=apply(api_client,case,value['previewId'])
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE',response.text
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id==case['user']))==1
