from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from app.core.clock import now_utc
from app.db import schema as t
from tests.test_material_storage import material_case
from tests.test_tutor_e2e import tutor_case, ask, history_of


def backup(client, case, kind='basic', user=None):
    return client.get('/api/tutor/backup', headers={'X-Anonymous-Id': user or case['user']}, params={'kind': kind})


def fail_message_write(client, case, *, history=None):
    from app.db.session import engine
    def fail(conn, cursor, statement, parameters, context, many):
        if context.isinsert and getattr(context.compiled.statement.table, 'name', None) == 'tutor_sources':
            raise RuntimeError('Injected write failure after gateway save')
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        result = ask(client, case, 'Explain simply' if history else 'Explain AVL rotations', history)
        assert result.status_code == 503, result.text
    finally:
        event.remove(engine, 'before_cursor_execute', fail)


@pytest.mark.parametrize('kind', ['basic', 'easy'])
def test_backup_recovers_exact_failed_request_without_external_calls(api_client, tutor_case, domain_connection, monkeypatch, kind):
    conn, case = domain_connection, tutor_case
    parent = ask(api_client, case) if kind == 'easy' else None
    if parent is not None:
        assert parent.status_code == 200, parent.text
    fail_message_write(api_client, case, history=history_of(parent) if parent is not None else None)
    before = conn.scalar(select(func.count()).select_from(t.ai_usage_logs))
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == (1 if kind == 'easy' else 0)
    conn.commit()
    monkeypatch.setenv('TUTOR_TEST_MODE', 'fail')
    monkeypatch.delenv('GROQ_API_KEY')
    recovered = backup(api_client, case, kind)
    assert recovered.status_code == 200, recovered.text
    value = recovered.json()['data']
    assert value['saved'] is True and value['question'] == 'Explain AVL rotations'
    assert value['parentId'] == (parent.json()['data']['id'] if parent is not None else None)
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == before
    count = conn.scalar(select(func.count()).select_from(t.tutor_messages))
    conn.commit()
    assert backup(api_client, case, kind).json() == recovered.json()
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == count


def test_latest_failed_question_does_not_reuse_an_older_different_answer(api_client, tutor_case, domain_connection, monkeypatch):
    conn, case = domain_connection, tutor_case
    fail_message_write(api_client, case)
    monkeypatch.setenv('TUTOR_TEST_MODE', 'fail')
    assert ask(api_client, case, 'A different AVL question').status_code == 503
    before = conn.scalar(select(func.count()).select_from(t.ai_usage_logs))
    conn.commit()
    result = backup(api_client, case)
    assert result.status_code == 503 and result.json()['error']['code'] == 'AI_FAILED'
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == before
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 0


def test_backup_rejects_changed_material_version_and_foreign_requests(api_client, tutor_case, domain_connection):
    conn, case = domain_connection, tutor_case
    fail_message_write(api_client, case)
    assert backup(api_client, case, user=case['other_user']).status_code == 503
    conn.execute(update(t.materials).where(t.materials.c.id == case['materialId']).values(analysis_version='new-version'))
    conn.commit()
    assert backup(api_client, case).status_code == 503
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 0


def test_ambiguous_exact_input_cache_is_not_arbitrarily_chosen(api_client, tutor_case, domain_connection):
    conn, case = domain_connection, tutor_case
    fail_message_write(api_client, case)
    existing = dict(conn.execute(select(t.ai_saved_results).where(t.ai_saved_results.c.kind == 'tutor')).mappings().one())
    other = {**existing, 'id': str(uuid4()), 'result': {**existing['result'], 'answer': 'Different saved answer'}}
    conn.execute(t.ai_saved_results.insert().values(**other))
    conn.commit()
    assert backup(api_client, case).status_code == 503
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 0


def test_no_failure_no_candidate_and_invalid_backup_kind(api_client, tutor_case, domain_connection):
    case, conn = tutor_case, domain_connection
    assert backup(api_client, case).status_code == 503
    assert backup(api_client, case, 'easy').status_code == 503
    assert backup(api_client, case, 'other').status_code == 422
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == 0
