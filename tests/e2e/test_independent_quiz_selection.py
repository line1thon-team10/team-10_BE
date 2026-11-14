"""Independent Q-07 HTTP checks at selection, sealed and source boundaries."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
from sqlalchemy import event, select, update
from app.db import schema as t, session
from app.services.quiz_selection import QuizSelection
from tests.test_progress_contract import progress_world
from tests.test_quiz_selection import add_question, get, prior_seed_quizzes


def test_concurrent_get_has_one_durable_set_and_retry_keeps_item_ids(api_client, progress_world, monkeypatch):
    w = progress_world
    unit = w.unit(1, 10)
    w.task((unit, 1, 10), done=[(unit, 1, 7)], status='partial')
    for _ in range(10): add_question(w, unit, page=7)
    w.conn.commit()
    if w.conn.dialect.name == 'sqlite':
        @event.listens_for(session.engine, 'checkout')
        def quick(connection, *args): connection.execute('PRAGMA busy_timeout=20')
    entered, release = Event(), Event()
    original = QuizSelection.create
    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(QuizSelection, 'create', held)
    def request(): return api_client.get(f"/api/quiz/{w.subject['id']}", headers={'X-Anonymous-Id': w.user})
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(request)
        try:
            assert entered.wait(5)
            second = request()
            assert second.status_code == 409, second.text
            assert second.json()['error']['code'] == 'REQUEST_IN_PROGRESS'
        finally:
            release.set()
        first = pending.result(timeout=5)
    assert first.status_code == 200, first.text
    assert request().json() == first.json()
    ids = list(w.conn.scalars(select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.user_id == w.user,
        t.quiz_sessions.c.quiz_date == w.day, t.quiz_sessions.c.subject_id == w.subject['id'])))
    assert ids == [first.json()['data']['quizId']]


def test_submitted_same_unit_extension_gets_new_five_but_sealed_set_cannot_expand(api_client, progress_world):
    w = progress_world
    unit = w.unit(1, 10)
    task = w.task((unit, 1, 10), done=[(unit, 1, 7)], status='partial')
    early = {add_question(w, unit, page=7) for _ in range(5)}
    later = {add_question(w, unit, page=9) for _ in range(5)}
    initial = get(api_client, w).json()['data']
    w.conn.execute(t.quiz_submission_states.insert().values(quiz_session_id=initial['quizId'],
        sealed_at=w.now, input_recovered=False, sealed_answers=[], legacy=True))
    w.conn.commit()
    patched = api_client.patch(f'/api/tasks/{task}/record', headers={'X-Anonymous-Id': w.user}, json={'toPage': 10})
    assert patched.status_code == 200, patched.text
    frozen = get(api_client, w)
    assert frozen.status_code == 200, frozen.text
    assert [q['id'] for q in frozen.json()['data']['questions']] == [q['id'] for q in initial['questions']]
    w.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == initial['quizId']).values(
        status='submitted', grading_status='completed', finalized_at=w.now))
    response = get(api_client, w)
    assert response.status_code == 200, response.text
    fresh = response.json()['data']
    assert fresh['quizId'] != initial['quizId'] and len(fresh['questions']) == 5
    selected = set(w.conn.scalars(select(t.quiz_items.c.question_id).where(t.quiz_items.c.quiz_session_id == fresh['quizId'])))
    assert selected == later and not selected & early
    assert get(api_client, w).json() == response.json()


def test_every_source_must_be_completed_and_shortage_rolls_back_all_units(api_client, progress_world):
    w = progress_world
    first, second = w.unit(1, 10), w.unit(1, 10)
    for unit in (first, second): w.task((unit, 1, 10), done=[(unit, 1, 7)], status='partial')
    for _ in range(5): add_question(w, first, page=7)
    for _ in range(4): add_question(w, second, page=7)
    invalid = add_question(w, second, page=7)
    w.conn.execute(t.question_sources.insert().values(id=str(uuid4()), question_id=invalid,
        material_id=second['material_id'], start_page=8, end_page=8, quote='아직 안 읽은 추가 근거'))
    before = w.conn.execute(select(t.quiz_sessions)).all()
    response = get(api_client, w)
    assert response.status_code == 404 and response.json()['error']['code'] == 'NO_QUESTIONS'
    assert w.conn.execute(select(t.quiz_sessions)).all() == before
