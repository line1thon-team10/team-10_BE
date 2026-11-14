"""HTTP upload -> persisted analysis -> original recovery -> binary download."""
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.clock import now_utc
from app.db import schema as t
from tests.test_material_storage import material_case, pdf_bytes, upload


@pytest.mark.parametrize('old_file_state', ['deleted', 'missing_key', 'both'])
@pytest.mark.parametrize('status', ['queued', 'running', 'succeeded'])
def test_reupload_restores_original_and_preserves_analysis_and_structure(
        api_client, material_case, domain_connection, old_file_state, status):
    case, conn, raw = material_case, domain_connection, pdf_bytes('restored original')
    first = upload(api_client, case, raw)
    assert first.status_code == 200, first.text
    accepted = first.json()['data']
    material = conn.execute(select(t.materials)).mappings().one()
    path = case['root'] / material['original_storage_key']
    path.unlink()
    now, unit_id, concept_id = now_utc(), str(uuid4()), str(uuid4())
    conn.execute(t.units.insert().values(id=unit_id, material_id=accepted['materialId'],
        code='original', title='Edited unit', sort_order=1, start_page=1, end_page=1,
        in_scope=False, created_at=now, updated_at=now))
    conn.execute(t.concepts.insert().values(id=concept_id, unit_id=unit_id, code='original',
        title='Edited concept', sort_order=1, start_page=1, end_page=1,
        created_at=now, updated_at=now))
    conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == accepted['jobId']).values(
        status=status, result={'preserved': True}))
    values = {'analysis_status': status, 'quiz_enabled': True, 'extraction_method': 'text'}
    if old_file_state in ('deleted', 'both'):
        values['original_deleted_at'] = now
    if old_file_state in ('missing_key', 'both'):
        values.update(original_storage_key=None, storage_key=None)
    conn.execute(update(t.materials).where(t.materials.c.id == accepted['materialId']).values(**values))
    structure = (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all())
    job = dict(conn.execute(select(t.ai_jobs)).mappings().one())
    conn.commit()
    missing = api_client.get(f"/api/materials/{accepted['materialId']}/file", headers=case['headers'])
    assert missing.status_code == 404
    again = upload(api_client, case, raw)
    assert again.status_code == 200 and again.json() == first.json(), again.text
    downloaded = api_client.get(f"/api/materials/{accepted['materialId']}/file", headers=case['headers'])
    assert downloaded.status_code == 200 and downloaded.content == raw
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all()) == structure
    assert dict(conn.execute(select(t.ai_jobs)).mappings().one()) == job
    restored = conn.execute(select(t.materials)).mappings().one()
    assert restored['original_deleted_at'] is None
    assert restored['original_storage_key'] == restored['storage_key']
    assert restored['analysis_status'] == status and restored['quiz_enabled']
    conn.commit()
    forbidden = api_client.get(f"/api/materials/{accepted['materialId']}/file",
        headers={'X-Anonymous-Id': case['other_user']})
    assert forbidden.status_code == 404
