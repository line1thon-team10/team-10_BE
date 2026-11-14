from tests.test_question_bank_preparation import bank_jobs, prepared_material
from tests.test_material_storage import material_case


def test_retry_cli_only_queues_owned_failed_job_without_external_call(
        domain_connection, prepared_material, monkeypatch, capsys):
    from app.workers.materials import main
    case, ids, worker, bank = prepared_material
    monkeypatch.setenv('BANK_E2E_TRANSPORT', 'failed')
    assert bank.run_next()
    job = bank_jobs(domain_connection)[0]
    domain_connection.commit()
    assert main(['--retry-bank-job', job['id'], '--user-id', case['other_user']]) == 1
    assert bank_jobs(domain_connection)[0]['status'] == 'failed'
    domain_connection.commit()
    assert main(['--retry-bank-job', job['id'], '--user-id', case['user']]) == 0
    assert bank_jobs(domain_connection)[0]['status'] == 'queued'
    domain_connection.commit()
    monkeypatch.setenv('BANK_E2E_TRANSPORT', 'valid')
    assert bank.run_next()
    assert bank_jobs(domain_connection)[0]['status'] == 'succeeded'
    domain_connection.commit()
    assert main(['--retry-bank-job', job['id'], '--user-id', case['user']]) == 0
    assert bank_jobs(domain_connection)[0]['status'] == 'succeeded'
