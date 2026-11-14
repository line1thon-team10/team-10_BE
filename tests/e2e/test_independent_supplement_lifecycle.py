"""Independent S02/S03 public mutation flows and adversarial persisted evidence."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.services.quiz import QuizService
from app.services.recovery_evidence import recovery_eligibility
from tests.test_mastery_rules import mastery_case, make_quiz, submit_quiz, DAY, AT
from tests.test_remedial_tasks import remedial_case


def low_quiz(client, conn, case):
    return submit_quiz(client, conn, case, case['questions'][0], 14)


def go(conn, case, day):
    conn.execute(update(t.users).where(t.users.c.id == case['user'].id).values(demo_date=day))
    conn.commit()


def pass_rows(conn):
    return [dict(row) for row in conn.execute(select(t.learning_passes).where(
        t.learning_passes.c.completion_kind == 'remedial')).mappings()]


def eligible(conn, case):
    data = QuizService(conn, case['user'].to_mapping(), case['nextday'])._metrics_input()
    return any(row.supplement_completed for row in recovery_eligibility(conn, case['user'].id, data))


def remedial_task(conn):
    return conn.execute(select(t.study_tasks).where(t.study_tasks.c.task_type == 'remedial')).mappings().one()


def write(client, case, task, *, partial=False, end=9, patch=False):
    body = {'toPage': end} if patch else {'taskId': task, 'result': 'partial' if partial else 'done'}
    if partial and not patch:
        body['toPage'] = end
    response = (client.patch(f'/api/tasks/{task}/record', headers=case['headers'], json=body) if patch
                else client.post('/api/records', headers=case['headers'], json=body))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('first_kind', ['initial', 'full_relearn'])
def test_quiz_obligation_record_correction_and_later_mastery_are_connected(api_client, domain_connection, remedial_case, first_kind):
    conn, case = domain_connection, remedial_case
    if first_kind == 'full_relearn':
        snap = dict(conn.scalar(select(t.learning_passes.c.evidence_snapshot).where(t.learning_passes.c.id == case['initial'])))
        snap['directRequestHash'] = 'd' * 64
        conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == case['initial']).values(
            completion_kind=first_kind, evidence_key='direct:' + 'e' * 64, evidence_snapshot=snap))
        source = conn.scalar(select(t.study_records.c.task_id).where(t.study_records.c.id == case['record']))
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == source).values(task_type='review', plan_id=None))
        conn.commit()
    source_quiz, result = low_quiz(api_client, conn, case)
    assert result['unitChanges'][0]['after'] == 70
    obligation = dict(conn.execute(select(t.remedial_obligations)).mappings().one())
    assert obligation['required_ranges'] == [[8, 10]]
    task = remedial_task(conn)
    go(conn, case, case['nextday'])
    write(api_client, case, task['id'], partial=True, end=9)
    assert pass_rows(conn) == [] and not eligible(conn, case)
    conn.commit()
    write(api_client, case, task['id'], patch=True, end=10)
    completed = pass_rows(conn)
    assert len(completed) == 1 and eligible(conn, case)
    assert completed[0]['evidence_key'] == obligation['id']
    assert completed[0]['evidence_snapshot']['sourceQuizId'] == source_quiz
    assert completed[0]['completed_date'] == case['nextday']
    conn.commit()
    q2, r2 = submit_quiz(api_client, conn, case, case['questions'][0], 20, day=case['nextday'])
    frozen = dict(conn.execute(select(t.mastery_history).where(t.mastery_history.c.quiz_session_id == q2)).mappings().one())
    assert frozen['score'] == 75 and frozen['daily_increase_limit'] == 5
    conn.commit()
    write(api_client, case, task['id'], patch=True, end=9)
    assert pass_rows(conn)[0]['invalidated_at'] is not None and not eligible(conn, case)
    assert dict(conn.execute(select(t.mastery_history).where(t.mastery_history.c.quiz_session_id == q2)).mappings().one()) == frozen
    go(conn, case, case['nextday'] + timedelta(days=1))
    _, r3 = submit_quiz(api_client, conn, case, case['questions'][0], 20, day=case['nextday'] + timedelta(days=1))
    assert r3['unitChanges'][0]['after'] == 79 and r3['unitChanges'][0]['grade'] == 'shaky'
    write(api_client, case, task['id'], patch=True, end=10)
    assert pass_rows(conn) == completed and eligible(conn, case)
    go(conn, case, case['nextday'] + timedelta(days=2))
    _, r4 = submit_quiz(api_client, conn, case, case['questions'][0], 20, day=case['nextday'] + timedelta(days=2))
    assert r4['unitChanges'][0]['after'] >= 80 and r4['unitChanges'][0]['grade'] == 'stable'
    assert dict(conn.execute(select(t.remedial_obligations)).mappings().one()) == obligation


@pytest.mark.parametrize('damage', ['foreign_quiz', 'unfinalized_quiz', 'early_record', 'foreign_record', 'other_scope', 'malformed_blocks'])
def test_recovery_rechecks_original_obligation_and_owned_evidence(api_client, domain_connection, remedial_case, damage):
    conn, case = domain_connection, remedial_case
    source, _ = low_quiz(api_client, conn, case)
    task = remedial_task(conn)
    go(conn, case, case['nextday'])
    write(api_client, case, task['id'])
    assert eligible(conn, case)
    learned = pass_rows(conn)[0]
    snap = dict(learned['evidence_snapshot'])
    if damage == 'foreign_quiz':
        other = str(uuid4())
        conn.execute(t.users.insert().values(id=other, created_at=AT, updated_at=AT))
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == source).values(user_id=other))
    elif damage == 'unfinalized_quiz':
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == source).values(finalized_at=None))
    elif damage == 'early_record':
        conn.execute(update(t.study_records).where(t.study_records.c.id.in_(snap['recordIds'])).values(study_date=DAY))
    elif damage == 'foreign_record':
        other = str(uuid4())
        conn.execute(t.users.insert().values(id=other, created_at=AT, updated_at=AT))
        conn.execute(update(t.subjects).where(t.subjects.c.id == case['subject_id']).values(user_id=other))
    elif damage == 'other_scope':
        conn.execute(update(t.concepts).where(t.concepts.c.unit_id == learned['unit_id'], t.concepts.c.sort_order == 3).values(end_page=9))
    else:
        snap['blockIds'] = [True, {'id': task['block_id']}]
        conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == learned['id']).values(evidence_snapshot=snap))
    conn.commit()
    assert not eligible(conn, case), damage
    # Eligibility probing is read-only and never rewrites the historical pass.
    assert pass_rows(conn)[0]['invalidated_at'] is None


def test_overlapping_wrong_concepts_are_unioned_and_unallocated_obligation_survives_correction(api_client, domain_connection, remedial_case):
    conn, case = domain_connection, remedial_case
    uid = case['units'][0]['id']
    conn.execute(update(t.concepts).where(t.concepts.c.unit_id == uid, t.concepts.c.sort_order == 1).values(end_page=5))
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['subject_id']).values(exam_date=DAY + timedelta(days=1)))
    conn.commit()
    quiz, result = submit_quiz(api_client, conn, case, case['questions'][0], 0)
    original = dict(conn.execute(select(t.remedial_obligations)).mappings().one())
    assert original['required_ranges'] == [[1, 6], [8, 10]]
    assert list(conn.scalars(select(t.remedial_obligation_blocks.c.block_id))) == []
    assert list(conn.scalars(select(t.study_tasks.c.id).where(t.study_tasks.c.task_type == 'remedial'))) == []
    task = conn.scalar(select(t.study_records.c.task_id).where(t.study_records.c.id == case['record']))
    ranges = conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == task).order_by(t.study_task_ranges.c.sort_order)).mappings().all()
    conn.commit()
    response = api_client.patch(f'/api/tasks/{task}/record', headers=case['headers'], json={'completedRanges': [{'rangeId': ranges[0]['id'], 'toPage': 3}]})
    assert response.status_code == 200, response.text
    assert conn.scalar(select(t.learning_passes.c.invalidated_at).where(t.learning_passes.c.id == case['initial'])) is not None
    assert dict(conn.execute(select(t.remedial_obligations)).mappings().one()) == original
    conn.commit()
    response = api_client.patch(f'/api/tasks/{task}/record', headers=case['headers'], json={'completedRanges': [{'rangeId': row['id'], 'toPage': row['end_page']} for row in ranges]})
    assert response.status_code == 200, response.text
    assert dict(conn.execute(select(t.remedial_obligations)).mappings().one()) == original
    assert list(conn.scalars(select(t.remedial_obligation_blocks.c.block_id))) == []


def test_finalized_carried_over_source_remains_valid_when_remedial_is_completed(api_client, domain_connection, remedial_case):
    conn, case = domain_connection, remedial_case
    source, _ = low_quiz(api_client, conn, case)
    task = remedial_task(conn)
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == source).values(status='carried_over'))
    go(conn, case, case['nextday'])
    write(api_client, case, task['id'])
    passes = pass_rows(conn)
    assert len(passes) == 1 and eligible(conn, case)
    assert passes[0]['evidence_snapshot']['sourceQuizId'] == source
