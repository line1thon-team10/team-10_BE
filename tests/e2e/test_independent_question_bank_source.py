"""Changed original PDF bytes cannot use previous analysis evidence."""
from sqlalchemy import func, select

from app.db import schema as t
from tests.test_question_bank_preparation import prepared_material, material_case, bank_jobs


def test_changed_pdf_bytes_cannot_create_bank_from_previous_analysis(api_client, domain_connection, prepared_material):
    conn = domain_connection
    case, ids, analysis, bank = prepared_material
    key = conn.scalar(select(t.materials.c.original_storage_key).where(t.materials.c.id == ids['materialId']))
    calls = conn.scalar(select(func.count()).select_from(t.ai_usage_logs)); conn.commit()
    path = case['root'] / key
    path.write_bytes(path.read_bytes() + b'\nchanged-source')
    assert bank.run_next()
    assert bank_jobs(conn)[0]['error_code'] == 'QUESTION_BANK_STALE'
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == calls
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 0
    assert conn.scalar(select(t.materials.c.analysis_status)) == 'succeeded'
