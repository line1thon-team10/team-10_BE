"""HTTP plus real isolated AI gateway, with independently specified model answers."""
import json

import pytest
from sqlalchemy import func, select, update

from app.ai import resplit_adapter
from app.ai.gateway import FunctionRef, ProviderSpec
from app.db import schema as t
from tests.test_plan_resplit_e2e import plan_case, addition_case, call, prepare, state


def ready(subject=None, cap=30, dates=None):
    return {'ready': True, 'question': None, 'windows': [{'subjectId': subject,
        'dates': dates or ['2026-11-17', '2026-11-18'], 'minutesPerDay': cap}]}


QUESTION = {'ready': False, 'question': {'text': '하루 몇 분인가요?', 'choices': ['하루30분']}, 'windows': []}


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'local-independent-transport')
    monkeypatch.setattr(resplit_adapter, 'PROVIDER', ProviderSpec(FunctionRef(
        'tests.e2e.resplit_static_transport', 'provider')))

    def output(first, second=None):
        monkeypatch.setenv('INDEPENDENT_RESPLIT_OUTPUTS', json.dumps([first, second or first]))
    return output


@pytest.mark.parametrize('raw_text,output', [
    ('내일부터 하루30분까지 다시 나눠줘', ready(cap=60)),
    ('내일부터 하루30분까지 다시 나눠줘', ready(cap=None)),
    ('내일은 공부하지 말고 모레 다시 나눠줘', ready(cap=None, dates=['2026-11-17'])),
])
def test_model_cannot_erase_original_cap_or_negation(api_client, domain_connection, addition_case, model, raw_text, output):
    conn, case = domain_connection, addition_case
    model(output)
    before = state(conn); conn.commit()
    result = prepare(api_client, case, request=raw_text)
    assert result.status_code == 503 and result.json()['error']['code'] == 'AI_FAILED', result.text
    assert state(conn) == before
    assert conn.scalar(select(func.count()).select_from(t.plan_proposal_states).where(
        t.plan_proposal_states.c.kind == 'remaining_plan_resplit')) == 0
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == 1


@pytest.mark.parametrize('second_question', [False, True])
def test_followup_is_once_and_preserves_original_dates_and_subject(api_client, domain_connection, addition_case, model, second_question):
    conn, case = domain_connection, addition_case
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['subject']).values(name='수학'))
    before = state(conn); conn.commit()
    model(QUESTION, QUESTION if second_question else ready(case['subject']))
    text = '수학 내일부터 공부 시간을 줄여줘'
    question = prepare(api_client, case, request=text)
    assert question.status_code == 200 and not question.json()['data']['ready'], question.text
    request_id = question.json()['data']['requestId']
    result = prepare(api_client, case, request=text, requestId=request_id, answer='하루30분')
    if second_question:
        assert result.status_code == 503 and result.json()['error']['code'] == 'AI_FAILED', result.text
        assert state(conn) == before
        return
    assert result.status_code == 200 and result.json()['data']['ready'], result.text
    assert prepare(api_client, case, request=text, requestId=request_id, answer='하루30분').json() == result.json()
    assert prepare(api_client, case, request=text, requestId=request_id, answer='하루60분').status_code == 422
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == 2
    assert state(conn) == before; conn.commit()
    identifier = result.json()['data']['previewId']
    applied = call(api_client, case, 'post', '/api/plan/resplit/apply', json={'previewId': identifier})
    assert applied.status_code == 200, applied.text
    tasks = conn.execute(select(t.study_tasks).where(t.study_tasks.c.status == 'planned')).mappings().all()
    assert tasks and sum(task['estimated_minutes'] for task in tasks) == 50
    for day in ['2026-11-17', '2026-11-18']:
        assert sum(task['estimated_minutes'] for task in tasks if task['scheduled_date'].isoformat() == day) <= 30
    assert all(task['scheduled_date'].isoformat() in ['2026-11-17', '2026-11-18'] for task in tasks)
