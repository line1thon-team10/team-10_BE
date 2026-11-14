from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select, update

from app.db import schema as t
from tests.test_quiz_answer_api import DAY, NOW, answer_case, make_case
from tests.test_quiz_finalize import get_result, submit


def child_case(conn, parent, *, kind="extra"):
    child = {**parent, "quiz": str(uuid4())}
    conn.execute(t.quiz_sessions.insert().values(id=child["quiz"], user_id=parent["user"],
        subject_id=parent["subject"], parent_quiz_session_id=parent["quiz"], type=kind,
        quiz_date=DAY, status="ready", created_at=NOW))
    conn.execute(t.quiz_scopes.insert().values(id=str(uuid4()), quiz_session_id=child["quiz"],
        unit_id=parent["unit"], start_page=1, end_page=10))
    for index, question_type in enumerate(("choice", "ox", "short", "essay"), 1):
        child[f"{question_type}_item"] = str(uuid4())
        conn.execute(t.quiz_items.insert().values(id=child[f"{question_type}_item"],
            quiz_session_id=child["quiz"], question_id=parent[question_type],
            sort_order=index, inclusion_reason="practice"))
    conn.commit()
    return child


def stored_states(conn):
    return [dict(row) for row in conn.execute(select(t.quiz_submission_states).order_by(
        t.quiz_submission_states.c.quiz_session_id)).mappings()]


def test_get_appends_frozen_child_without_changing_parent_or_post_replay(
        api_client, answer_case, domain_connection):
    parent, conn = answer_case, domain_connection
    first = submit(api_client, parent)
    assert first.status_code == 200, first.text
    original = first.json()
    assert original["data"]["more"] == []
    child = child_case(conn, parent)
    completed = submit(api_client, child)
    assert completed.status_code == 200, completed.text
    fixed_child = completed.json()["data"]
    frozen = deepcopy(stored_states(conn))
    # A later read must use both frozen cores, not recalculate changed metadata.
    conn.execute(update(t.units).where(t.units.c.id == parent["unit"]).values(mastery_score=1, title="변경"))
    conn.execute(update(t.questions).where(t.questions.c.id == parent["choice"]).values(prompt="변경"))
    conn.commit()
    current = get_result(api_client, parent)
    assert current.status_code == 200, current.text
    assert current.json()["data"] == {**original["data"], "more": [fixed_child]}
    assert get_result(api_client, child).json() == completed.json()
    assert submit(api_client, parent).json() == original
    assert stored_states(conn) == frozen


def test_get_excludes_foreign_unfinished_and_unpublished_children(
        api_client, answer_case, domain_connection):
    parent, conn = answer_case, domain_connection
    first = submit(api_client, parent)
    assert first.status_code == 200, first.text
    child = child_case(conn, parent)
    completed = submit(api_client, child)
    assert completed.status_code == 200, completed.text
    foreign = make_case(conn)
    foreign_result = submit(api_client, foreign)
    assert foreign_result.status_code == 200, foreign_result.text
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == foreign["quiz"]).values(
        parent_quiz_session_id=parent["quiz"]))
    conn.commit()
    pending = child_case(conn, parent)
    unpublished = child_case(conn, parent)
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == unpublished["quiz"]).values(
        status="submitted", grading_status="completed", finalized_at=NOW))
    conn.execute(t.quiz_submission_states.insert().values(quiz_session_id=unpublished["quiz"],
        sealed_at=NOW, input_recovered=False, sealed_answers=[], legacy=True))
    conn.commit()
    expected = {**first.json()["data"], "more": [completed.json()["data"]]}
    assert get_result(api_client, parent).json()["data"] == expected
    assert get_result(api_client, pending).status_code == 404
    # Each completion marker is required even if an old snapshot exists.
    finished = dict(conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.id == child["quiz"])).mappings().one())
    conn.commit()
    for field, value in (("status", "in_progress"), ("grading_status", "processing"), ("finalized_at", None)):
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == child["quiz"]).values({field: value}))
        conn.commit()
        assert get_result(api_client, parent).json() == first.json()
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == child["quiz"]).values({field: finished[field]}))
        conn.commit()
    assert get_result(api_client, foreign).json() == foreign_result.json()
    assert conn.execute(select(t.quiz_submission_states.c.result_snapshot).where(
        t.quiz_submission_states.c.quiz_session_id == unpublished["quiz"])).scalar() is None


def test_get_excludes_cycles_without_mutating_either_snapshot(api_client, answer_case, domain_connection):
    parent, conn = answer_case, domain_connection
    first = submit(api_client, parent)
    assert first.status_code == 200, first.text
    child = child_case(conn, parent, kind="practice")
    completed = submit(api_client, child)
    assert completed.status_code == 200, completed.text
    frozen = deepcopy(stored_states(conn))
    # Corrupt historical parent links must not recursively include the ancestor.
    for parent_id in (child["quiz"], parent["quiz"]):
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == parent["quiz"]).values(
            parent_quiz_session_id=parent_id))
        conn.commit()
        result = get_result(api_client, parent)
        assert result.status_code == 200, result.text
        assert result.json()["data"] == {**first.json()["data"], "more": [completed.json()["data"]]}
        assert submit(api_client, parent).json() == first.json()
        assert stored_states(conn) == frozen
    conn.commit()
