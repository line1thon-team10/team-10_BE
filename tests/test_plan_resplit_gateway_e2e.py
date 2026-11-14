"""HTTP/DB/gateway/isolated worker are real; only Groq transport is replaced."""
from sqlalchemy import select, update, func, event
import pytest
from app.ai.gateway import FunctionRef, ProviderSpec
from app.ai import resplit_adapter
from app.db import schema as t
from tests.test_plan_resplit_e2e import NOW,TODAY,plan_case,addition_case,state,call,prepare


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY','local-transport-fixture')
    monkeypatch.setattr(resplit_adapter,'PROVIDER',ProviderSpec(FunctionRef('tests.resplit_transport_fixture','resplit_transport_provider')))


def usage(conn):
    return conn.scalar(select(func.count()).select_from(t.ai_usage_logs))


def test_real_gateway_interprets_dates_and_reuses_owned_candidate(api_client,domain_connection,addition_case,monkeypatch):
    conn,case=domain_connection,addition_case
    response=prepare(api_client,case,request='내일부터 다시 나눠줘')
    assert response.status_code==200 and response.json()['data']['ready'],response.text
    assert usage(conn)==1
    row=conn.execute(select(t.ai_usage_logs)).mappings().one()
    assert row['input_tokens']==13 and row['output_tokens']==7
    conn.commit()
    monkeypatch.delenv('GROQ_API_KEY')
    monkeypatch.setenv('RESPLIT_FIXTURE_BEHAVIOR','failure')
    assert prepare(api_client,case,request='내일부터 다시 나눠줘',saved=True).json()==response.json()
    assert usage(conn)==1;conn.commit()
    identifier=response.json()['data']['previewId']
    assert call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier}).status_code==200
    days=conn.scalars(select(t.study_tasks.c.scheduled_date).where(t.study_tasks.c.status=='planned')).all()
    assert days and all(day>TODAY for day in days)


@pytest.mark.parametrize('behavior',['fabricated','failure','syntax_always'])
def test_transport_failures_do_not_publish_or_mutate_plan(api_client,domain_connection,addition_case,monkeypatch,behavior):
    conn,case=domain_connection,addition_case
    before=state(conn);conn.commit()
    monkeypatch.setenv('RESPLIT_FIXTURE_BEHAVIOR',behavior)
    response=prepare(api_client,case,request='내일부터 다시 나눠줘')
    assert response.status_code==503 and response.json()['error']['code']=='AI_FAILED',response.text
    assert state(conn)==before
    assert conn.scalar(select(func.count()).select_from(t.plan_proposal_states).where(t.plan_proposal_states.c.kind=='remaining_plan_resplit'))==0
    assert usage(conn)==(2 if behavior=='syntax_always' else 1)


def test_real_short_followup_keeps_subject_and_time_cap(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['subject']).values(name='수학'));conn.commit()
    response=prepare(api_client,case,request='수학 공부 시간을 줄여줘')
    assert response.status_code==200 and not response.json()['data']['ready'],response.text
    request_id=response.json()['data']['requestId']
    response=prepare(api_client,case,request='수학 공부 시간을 줄여줘',requestId=request_id,answer='하루30분')
    assert response.status_code==200 and response.json()['data']['ready'],response.text
    compare=call(api_client,case,'get','/api/plan/resplit',params={'previewId':response.json()['data']['previewId']}).json()['data']
    assert all(row['after']<=30 for row in compare['days'])
    assert usage(conn)==2


def test_saved_request_recovers_validated_cache_after_candidate_write_rollback(api_client,domain_connection,addition_case,monkeypatch):
    from app.db import session
    conn,case=domain_connection,addition_case
    before=state(conn);conn.commit()
    def fail_candidate(connection,cursor,sql,*args):
        if sql.startswith('INSERT INTO plan_proposals '):
            raise RuntimeError('candidate write fault')
    event.listen(session.engine,'before_cursor_execute',fail_candidate)
    try:
        response=prepare(api_client,case,request='내일부터 다시 나눠줘')
    finally:
        event.remove(session.engine,'before_cursor_execute',fail_candidate)
    assert response.status_code==503,response.text
    assert state(conn)==before and usage(conn)==1;conn.commit()
    monkeypatch.delenv('GROQ_API_KEY')
    monkeypatch.setenv('RESPLIT_FIXTURE_BEHAVIOR','failure')
    response=prepare(api_client,case,request='내일부터 다시 나눠줘',saved=True)
    assert response.status_code==200 and response.json()['data']['ready'],response.text
    assert usage(conn)==1
    view=call(api_client,case,'get','/api/plan/resplit',params={'previewId':response.json()['data']['previewId']})
    assert view.json()['data']['saved'] is True


def test_saved_without_matching_result_never_calls_provider(api_client,domain_connection,addition_case,monkeypatch):
    conn,case=domain_connection,addition_case
    monkeypatch.delenv('GROQ_API_KEY')
    response=prepare(api_client,case,request='내일부터 다시 나눠줘',saved=True)
    assert response.status_code==503 and response.json()['error']['code']=='AI_FAILED'
    assert usage(conn)==0
