"""Integrated V04 -> custom quiz -> S02 -> B02 -> S03, using only public mutations."""
from uuid import uuid4
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_mastery_rules import mastery_case
from tests.test_remedial_tasks import remedial_case


def test_first_http_relearn_and_generated_supplement_unlock_exact_full_scope_bonus(api_client, domain_connection, remedial_case):
    conn, c = domain_connection, remedial_case
    uid = c['units'][0]['id']
    conn.execute(t.learning_passes.delete().where(t.learning_passes.c.unit_id == uid))
    conn.execute(update(t.units).where(t.units.c.id == uid).values(evidence=['원문에서 확인한 개념']))
    conn.execute(update(t.materials).where(t.materials.c.id == c['units'][0]['material_id']).values(page_count=10, analysis_status='succeeded', quiz_enabled=True))
    for question in conn.execute(select(t.questions).where(t.questions.c.unit_id == uid)).mappings():
        concept = conn.execute(select(t.concepts).where(t.concepts.c.id == question['concept_id'])).mappings().one()
        conn.execute(t.question_sources.insert().values(id=str(uuid4()), question_id=question['id'],
            material_id=c['units'][0]['material_id'], start_page=concept['start_page'], end_page=concept['end_page'], quote='확인한 원문 근거'))
    conn.commit()
    def relearn():
        key = str(uuid4())
        body = {'subjectId': c['subject_id'], 'unitId': uid}
        headers = {**c['headers'], 'Idempotency-Key': key}
        first = api_client.post('/api/review/relearn', headers=headers, json=body)
        assert first.status_code == 200, first.text
        assert api_client.post('/api/review/relearn', headers=headers, json=body).json() == first.json()
        return first.json()['data']
    def custom(correct):
        made = api_client.post('/api/review/custom-quiz', headers=c['headers'], json={
            'subjectId': c['subject_id'], 'scopes': [{'unitId': uid, 'from': a, 'to': b} for a, b in [(1,6),(8,10)]],
            'difficulty': 'low', 'count': 20, 'weight': 'even'})
        assert made.status_code == 200, made.text
        quiz = made.json()['data']['quizId']
        shown = api_client.get('/api/quiz/' + quiz, headers=c['headers'])
        assert shown.status_code == 200, shown.text
        items = shown.json()['data']['questions']
        assert len(items) == 20
        body = {'quizId': quiz, 'answers': [{'questionId': item['id'], 'choice': 0 if i < correct else 1, 'confidence': 'sure'} for i, item in enumerate(items)]}
        result = api_client.post('/api/quiz/' + quiz + '/submit', headers=c['headers'], json=body)
        assert result.status_code == 200, result.text
        assert api_client.post('/api/quiz/' + quiz + '/submit', headers=c['headers'], json=body).json() == result.json()
        return quiz, result.json()['data']
    assert relearn()['round'] == 1
    source, first = custom(14)
    assert first['unitChanges'][0]['after'] == 70
    obligation = dict(conn.execute(select(t.remedial_obligations)).mappings().one())
    assert obligation['quiz_session_id'] == source
    tasks = list(conn.scalars(select(t.study_tasks.c.id).where(t.study_tasks.c.task_type == 'remedial')))
    assert tasks
    conn.execute(update(t.users).where(t.users.c.id == c['user'].id).values(demo_date=c['nextday']))
    conn.commit()
    for task in tasks:
        done = api_client.post('/api/records', headers=c['headers'], json={'taskId': task, 'result': 'done'})
        assert done.status_code == 200, done.text
    learned = list(conn.execute(select(t.learning_passes).where(t.learning_passes.c.unit_id == uid).order_by(t.learning_passes.c.pass_number)).mappings())
    assert [row['completion_kind'] for row in learned] == ['full_relearn', 'remedial']
    assert learned[1]['evidence_key'] == obligation['id']
    conn.commit()
    assert relearn()['round'] == 3
    quiz, result = custom(20)
    history = conn.execute(select(t.mastery_history).where(t.mastery_history.c.quiz_session_id == quiz)).mappings().one()
    assert history['daily_increase_limit'] == 15 and history['score'] == 85
    assert result['unitChanges'][0]['grade'] == 'stable'
    assert list(conn.scalars(select(t.learning_passes.c.id).where(t.learning_passes.c.completion_kind == 'initial'))) == []
    assert dict(conn.execute(select(t.remedial_obligations)).mappings().one()) == obligation
