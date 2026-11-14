"""Independent source identity, no-write and stale-input interpretation checks."""
import pytest
from sqlalchemy import func, select, update

from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_record_interpret import interpretation_case, interpret, snapshot


def test_existing_completed_record_and_passes_are_unchanged_by_zero_or_partial_candidate(api_client, interpretation_case):
    case = interpretation_case
    world = case['world']
    saved = api_client.post('/api/records', headers={'X-Anonymous-Id': world.user}, json={'taskId': case['task'], 'result': 'done'})
    assert saved.status_code == 200, saved.text
    before = snapshot(world.conn)
    world.conn.commit()
    zero = interpret(api_client, case, '아직 못 했어')
    assert zero.status_code == 200 and zero.json()['data']['completedRanges'] == [], zero.text
    partial = interpret(api_client, case, '13쪽까지 읽었어')
    assert partial.status_code == 200 and partial.json()['data']['to'] == 13, partial.text
    assert snapshot(world.conn) == before
    stored = world.conn.execute(select(t.study_record_ranges).join(t.study_records,
        t.study_records.c.id == t.study_record_ranges.c.record_id).where(t.study_records.c.task_id == case['task'])).mappings().one()
    assert (stored['start_page'], stored['end_page']) == (10, 20)


@pytest.mark.parametrize('tampered', [False, True])
def test_reversed_pdf_mentions_still_use_task_order_and_cannot_swap_endpoints(api_client, interpretation_case, tampered):
    case = interpretation_case
    world = case['world']
    second = world.unit(10, 20)
    world.conn.execute(update(t.materials).where(t.materials.c.id == second['material_id']).values(original_name='교재B.pdf'))
    case['task'] = world.task((case['unit'], 10, 20), (second, 10, 20))
    rows = world.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == case['task'])
        .order_by(t.study_task_ranges.c.sort_order)).mappings().all()
    before = snapshot(world.conn)
    world.conn.commit()
    if tampered:
        def wrong(value, context):
            value['completedRanges'] = [{'rangeId': rows[0]['id'], 'toPage': 17}, {'rangeId': rows[1]['id'], 'toPage': 13}]
            return value
        case['model'].transform = wrong
    response = interpret(api_client, case, '교재B.pdf 17쪽까지, 교재A.pdf 13쪽까지 읽었어')
    if tampered:
        assert response.status_code == 503 and response.json()['error']['code'] == 'AI_FAILED', response.text
    else:
        assert response.status_code == 200, response.text
        assert response.json()['data']['from'] is None and response.json()['data']['to'] is None
        assert response.json()['data']['completedRanges'] == [{'rangeId': rows[0]['id'], 'toPage': 13}, {'rangeId': rows[1]['id'], 'toPage': 17}]
    assert snapshot(world.conn) == before


@pytest.mark.parametrize('changed', ['plan', 'range', 'retirement'])
def test_plan_range_or_unit_replacement_during_interpretation_rejects_candidate(api_client, interpretation_case, changed):
    case = interpretation_case
    world = case['world']
    def change(engine):
        with engine.begin() as conn:
            if changed == 'plan':
                conn.execute(update(t.plans).where(t.plans.c.id == world.plan['id']).values(revision=t.plans.c.revision+1))
            elif changed == 'range':
                conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id == case['task']).values(start_page=11))
            else:
                conn.execute(update(t.units).where(t.units.c.id == case['unit']['id']).values(retired_at=world.now))
    case['model'].callback = change
    before = world.conn.execute(select(t.study_records)).all()
    world.conn.commit()
    response = interpret(api_client, case, '13쪽까지 읽었어')
    assert response.status_code == 409 and response.json()['error']['code'] == 'INPUT_STALE', response.text
    assert world.conn.execute(select(t.study_records)).all() == before


def test_real_gateway_transport_validates_candidate_without_domain_writes(api_client, interpretation_case, monkeypatch):
    from app.ai.gateway import AiGateway, FunctionRef, ProviderSpec
    from app.services import record_interpret
    case = interpretation_case
    conn = case['world'].conn
    monkeypatch.setattr(record_interpret, 'AiGateway', AiGateway)
    monkeypatch.setattr(record_interpret, 'PROVIDER', ProviderSpec(FunctionRef('tests.e2e.independent_record_transport', 'provider')))
    monkeypatch.setenv('GROQ_API_KEY', 'fixture-only-not-a-real-key')
    before = snapshot(conn)
    conn.commit()
    response = interpret(api_client, case, '13쪽까지 읽었어')
    assert response.status_code == 200, response.text
    assert response.json()['data']['to'] == 13
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs).where(t.ai_usage_logs.c.kind == 'nl_parse')) == 1
    assert snapshot(conn) == before
