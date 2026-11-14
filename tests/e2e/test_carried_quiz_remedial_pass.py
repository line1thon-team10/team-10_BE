"""A real day-close finalization must remain usable by remedial learning."""
from sqlalchemy import func, select, update

from app.db import schema as t
from app.services.quiz import QuizService
from app.services.recovery_evidence import recovery_eligibility
from tests.test_mastery_rules import mastery_case, make_quiz, DAY
from tests.test_remedial_tasks import remedial_case


def test_partial_daily_quiz_close_generates_a_completable_remedial_pass(api_client, domain_connection, remedial_case):
    conn, case = domain_connection, remedial_case
    conn.execute(update(t.units).where(t.units.c.id == case['units'][0]['id']).values(evidence=True))
    conn.execute(update(t.materials).where(t.materials.c.id == case['units'][0]['material_id']).values(quiz_enabled=True))
    quiz, items = make_quiz(conn, case, case['questions'][0], day=DAY)
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == quiz).values(type='daily'))
    conn.commit()
    saved = api_client.put(f'/api/quiz/{quiz}/answers/{items[0]["id"]}', headers=case['headers'],
        json={'choice': 1, 'confidence': 'sure', 'revision': 0})
    assert saved.status_code == 200, saved.text
    from app.db.session import engine
    from app.services.grading import GradingService
    jobs = list(conn.scalars(select(t.quiz_grading_jobs.c.id).join(t.quiz_answers, t.quiz_grading_jobs.c.answer_id == t.quiz_answers.c.id).join(t.quiz_items, t.quiz_answers.c.quiz_item_id == t.quiz_items.c.id).where(t.quiz_items.c.quiz_session_id == quiz)))
    conn.commit()
    assert jobs
    for job in jobs:
        GradingService(engine, owner='remedial-carry-test').run_job(case['user'].id, job)
    closed = api_client.post('/api/demo/next-day', headers=case['headers'], json={})
    assert closed.status_code == 200, closed.text
    result = api_client.get(f'/api/quiz/{quiz}/result', headers=case['headers'])
    assert result.status_code == 200, result.text
    assert result.json()['data']['summary']['total'] == 1
    source = conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.id == quiz)).mappings().one()
    assert source['status'] == 'carried_over' and source['finalized_at'] is not None
    assert source['grading_status'] == 'completed' and source['submitted_at'] is None
    obligation = conn.execute(select(t.remedial_obligations).where(t.remedial_obligations.c.quiz_session_id == quiz)).mappings().one()
    tasks = conn.execute(select(t.study_tasks).join(t.remedial_obligation_blocks,
        t.study_tasks.c.block_id == t.remedial_obligation_blocks.c.block_id).where(
        t.remedial_obligation_blocks.c.obligation_id == obligation['id'])).mappings().all()
    assert tasks
    conn.execute(update(t.users).where(t.users.c.id == case['user'].id).values(demo_date=max(row['scheduled_date'] for row in tasks)))
    conn.commit()
    for task in tasks:
        done = api_client.post('/api/records', headers=case['headers'], json={'taskId': task['id'], 'result': 'done'})
        assert done.status_code == 200, done.text
    learned = conn.execute(select(t.learning_passes).where(t.learning_passes.c.completion_kind == 'remedial',
        t.learning_passes.c.evidence_key == obligation['id'])).mappings().all()
    assert len(learned) == 1
    assert learned[0]['evidence_snapshot']['sourceQuizId'] == quiz
    metrics = QuizService(conn, case['user'].to_mapping(), learned[0]['completed_date'])._metrics_input()
    assert any(row.supplement_completed for row in recovery_eligibility(conn, case['user'].id, metrics))
    conn.commit()
    replay = api_client.post('/api/records', headers=case['headers'], json={'taskId': tasks[-1]['id'], 'result': 'done'})
    assert replay.status_code == 200, replay.text
    assert conn.scalar(select(func.count()).select_from(t.learning_passes).where(t.learning_passes.c.completion_kind == 'remedial')) == 1
    assert api_client.get(f'/api/quiz/{quiz}/result', headers=case['headers']).json() == result.json()
