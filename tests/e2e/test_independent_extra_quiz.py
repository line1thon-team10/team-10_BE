"""Independent Q-09 guards for result publication and immutable retries."""
import pytest
from sqlalchemy import null, select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_quiz_selection import prior_seed_quizzes
from tests.test_extra_quiz import base, more


@pytest.mark.parametrize('guard', ['unfinalized', 'unpublished_snapshot'])
def test_more_requires_published_final_parent_result(api_client, progress_world, guard):
    w = progress_world
    unit, parent = base(api_client, w, 10)
    if guard == 'unfinalized':
        w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == parent).values(finalized_at=None))
    else:
        w.conn.execute(update(t.quiz_submission_states).where(t.quiz_submission_states.c.quiz_session_id == parent).values(
            result_snapshot=null(), result_provenance=None, result_schema_version=None, result_captured_at=None))
    before = w.conn.execute(select(t.quiz_sessions)).all()
    response = more(api_client, w, unit)
    assert response.status_code == 404, response.text
    assert response.json()['error']['code'] == 'NO_RESULT'
    assert w.conn.execute(select(t.quiz_sessions)).all() == before


def test_extra_replay_keeps_five_ids_after_scope_correction_and_cannot_cross_owner(api_client, progress_world):
    w = progress_world
    unit, parent = base(api_client, w, 10)
    response = more(api_client, w, unit)
    assert response.status_code == 200, response.text
    created = response.json()['data']
    original = w.conn.execute(select(t.quiz_items).where(t.quiz_items.c.quiz_session_id == created['quizId'])).all()
    task = w.conn.scalar(select(t.study_tasks.c.id).join(t.study_task_ranges,
        t.study_task_ranges.c.task_id == t.study_tasks.c.id).where(t.study_task_ranges.c.unit_id == unit['id']))
    w.conn.commit()
    changed = api_client.patch(f'/api/tasks/{task}/record', headers={'X-Anonymous-Id': w.user}, json={'completedRanges': []})
    assert changed.status_code == 200, changed.text
    assert more(api_client, w, unit).json() == response.json()
    assert w.conn.execute(select(t.quiz_items).where(t.quiz_items.c.quiz_session_id == created['quizId'])).all() == original
    w.conn.commit()
    other = api_client.post('/api/session').json()['data']['anonymousId']
    foreign = api_client.post(f"/api/quiz/{w.subject['id']}/more", headers={'X-Anonymous-Id': other}, json={'unitId': unit['id']})
    assert foreign.status_code == 404, foreign.text
