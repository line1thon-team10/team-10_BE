"""Real isolated gateway result survives atomic bank publication rollback."""
from sqlalchemy import event, func, select

from app.db import schema as t, session
from tests.test_question_bank_preparation import prepared_material, material_case, bank_jobs


def test_bank_save_failure_retries_validated_result_without_new_external_call(api_client, domain_connection, prepared_material, monkeypatch):
    conn = domain_connection
    case, ids, analysis, bank = prepared_material

    def fail_source(connection, cursor, sql, parameters, context, many):
        if context.isinsert and getattr(context.compiled.statement.table, 'name', None) == 'question_sources':
            raise RuntimeError('independent source insertion failure')

    event.listen(session.engine, 'before_cursor_execute', fail_source)
    try:
        assert bank.run_next()
    finally:
        event.remove(session.engine, 'before_cursor_execute', fail_source)
    job = bank_jobs(conn)[0]
    assert job['status'] == 'failed'
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 0
    assert conn.scalar(select(func.count()).select_from(t.question_sources)) == 0
    assert conn.scalar(select(t.ai_jobs.c.status).where(t.ai_jobs.c.id == ids['jobId'])) == 'succeeded'
    calls = conn.scalar(select(func.count()).select_from(t.ai_usage_logs)); conn.commit()
    monkeypatch.setenv('BANK_E2E_TRANSPORT', 'failed')
    assert bank.retry(case['user'], job['id'])
    assert bank.run_job(case['user'], job['id'])
    retried = bank_jobs(conn)[0]
    assert retried['id'] == job['id'] and retried['status'] == 'succeeded'
    assert retried['result']['reused'] is True
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == calls
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 10
    assert conn.scalar(select(func.count()).select_from(t.question_sources)) == 10

