from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_quiz_selection import add_question, get, prior_seed_quizzes
from tests.test_progress_contract import progress_world


@pytest.mark.parametrize('change', ['none', 'sha', 'version', 'reanalysis', 'failed_bank', 'foreign_owner', 'bad_id'])
def test_fresh_selection_requires_current_owned_bank_provenance(api_client,progress_world,change):
    w = progress_world; unit = w.unit(1,10)
    w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    material = dict(w.conn.execute(select(t.materials).where(t.materials.c.id == unit['material_id'])).mappings().one())
    analysis_id, bank_id = str(uuid4()), str(uuid4())
    w.conn.execute(update(t.materials).where(t.materials.c.id == material['id']).values(analysis_status='succeeded'))
    common = dict(user_id=w.user, status='succeeded', input_hash='a'*64, schema_version='test', created_at=w.now)
    w.conn.execute(t.ai_jobs.insert().values(id=analysis_id, kind='pdf_analysis', target_type='material',
        target_id=material['id'], idempotency_key=analysis_id, input_payload={}, **common))
    payload = {'unitId':unit['id'], 'materialId':material['id'], 'sourceSha256':material['sha256'],
        'analysisVersion':material['analysis_version'], 'analysisJobId':analysis_id, 'difficulty':'low'}
    w.conn.execute(t.ai_jobs.insert().values(id=bank_id, kind='question_pregenerate', target_type='unit',
        target_id=unit['id'], idempotency_key=bank_id, input_payload=payload, **common))
    for _ in range(5): add_question(w,unit,generation_version='question-bank-v1:'+bank_id)
    if change == 'sha': w.conn.execute(update(t.materials).where(t.materials.c.id == material['id']).values(sha256='b'*64))
    if change == 'version': w.conn.execute(update(t.materials).where(t.materials.c.id == material['id']).values(analysis_version='new'))
    if change == 'reanalysis':
        latest = str(uuid4())
        w.conn.execute(t.ai_jobs.insert().values(id=latest,kind='pdf_analysis',target_type='material',
            target_id=material['id'],idempotency_key=latest,input_payload={},**{**common,'created_at':w.now+timedelta(seconds=1)}))
    if change == 'failed_bank': w.conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == bank_id).values(status='failed'))
    if change == 'foreign_owner':
        other = str(uuid4())
        w.conn.execute(t.users.insert().values(id=other,created_at=w.now,updated_at=w.now))
        w.conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == bank_id).values(user_id=other))
    if change == 'bad_id': w.conn.execute(update(t.questions).where(t.questions.c.unit_id == unit['id']).values(generation_version='question-bank-v1:bad'))
    response = get(api_client,w)
    assert response.status_code == (200 if change == 'none' else 404), response.text
    if change != 'none': assert response.json()['error']['code'] == 'NO_QUESTIONS'
