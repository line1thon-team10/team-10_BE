"""A completed extra quiz must remain visible below the frozen original result."""
from uuid import uuid4

from sqlalchemy import select

from app.db import schema as t


def answers_for(quiz):
    return [{"questionId": q["id"], "confidence": "sure",
             **({"choice": 0} if q["type"] == "choice" else {"text": ""})}
            for q in quiz["questions"]]


def test_completed_extra_remains_visible_under_original_result(api_client, demo_user, domain_connection):
    headers = {"X-Anonymous-Id": demo_user}
    active = api_client.get('/api/study/sessions/active', headers=headers).json()['data']
    ended = api_client.post(f"/api/study/sessions/{active['id']}/end", headers=headers)
    assert ended.status_code == 200, ended.text
    recorded = api_client.post('/api/records', headers=headers, json={
        'taskId': active['taskId'], 'sessionId': active['id'], 'result': 'partial', 'toPage': 13})
    assert recorded.status_code == 200, recorded.text
    opened = api_client.get(f"/api/quiz/{active['subjectId']}", headers=headers)
    assert opened.status_code == 200, opened.text
    quiz = opened.json()['data']
    submitted = api_client.post(f"/api/quiz/{active['subjectId']}/submit", headers=headers,
        json={'quizId': quiz['quizId'], 'answers': answers_for(quiz)})
    assert submitted.status_code == 200, submitted.text
    original = submitted.json()['data']
    assert original['more'] == []
    # Prepare the documented >=5 unused questions precondition. The demo seed
    # itself has already consumed the unit's small question pool.
    conn = domain_connection
    item_id = next(q['id'] for q in quiz['questions'] if q['unitNo'] == 2 and q['type'] == 'choice')
    template = dict(conn.execute(select(t.questions).join(t.quiz_items,
        t.quiz_items.c.question_id == t.questions.c.id).where(t.quiz_items.c.id == item_id)).mappings().one())
    sources = [dict(row) for row in conn.execute(select(t.question_sources).where(
        t.question_sources.c.question_id == template['id'])).mappings()]
    for index in range(5):
        new_id = str(uuid4())
        conn.execute(t.questions.insert().values(**{**template, 'id': new_id, 'code': f'independent-extra-{index}'}))
        for source in sources:
            conn.execute(t.question_sources.insert().values(**{**source, 'id': str(uuid4()), 'question_id': new_id}))
    conn.commit()
    more = api_client.post(f"/api/quiz/{active['subjectId']}/more", headers=headers, json={'unitId': template['unit_id']})
    assert more.status_code == 200, more.text
    child_id = more.json()['data']['quizId']
    child_open = api_client.get(f'/api/quiz/{child_id}', headers=headers)
    assert child_open.status_code == 200, child_open.text
    child_submit = api_client.post(f'/api/quiz/{child_id}/submit', headers=headers,
        json={'quizId': child_id, 'answers': answers_for(child_open.json()['data'])})
    assert child_submit.status_code == 200, child_submit.text
    child_result = api_client.get(f'/api/quiz/{child_id}/result', headers=headers)
    assert child_result.status_code == 200, child_result.text
    reread = api_client.get(f"/api/quiz/{active['subjectId']}/result", headers=headers,
        params={'quizId': quiz['quizId']})
    assert reread.status_code == 200, reread.text
    result = reread.json()['data']
    for key in ('items', 'summary', 'unitChanges', 'bookChanges'):
        assert result[key] == original[key]
    assert [q['quizId'] for q in result['more']] == [child_id], {
        'parent_more': result['more'], 'child_status': child_result.status_code,
        'child_quiz_id': child_result.json()['data']['quizId']}
