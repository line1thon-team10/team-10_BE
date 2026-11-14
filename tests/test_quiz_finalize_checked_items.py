import pytest
from sqlalchemy import event, update

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.quiz_answers import QuizAnswerRepository
from tests.test_quiz_answer_api import answer_case, make_case


@pytest.fixture
def whole_item_reads(domain_connection):
    reads = []
    def observed(conn, cursor, statement, parameters, context, many):
        if "FROM quiz_items JOIN questions" in statement:
            reads.append(statement)
    event.listen(domain_connection.engine, "before_cursor_execute", observed)
    yield reads
    event.remove(domain_connection.engine, "before_cursor_execute", observed)


def test_checked_items_share_one_owned_read_and_copy_nested_values(domain_connection, answer_case, whole_item_reads):
    conn, case = domain_connection, answer_case
    repo = QuizAnswerRepository(conn)
    with repo.checked_items(case["user"], case["quiz"]):
        for _ in range(10):
            row = QuizAnswerRepository(conn).item(case["user"], case["quiz"], case["choice_item"])
            assert row["choices"][0]["text"] == "맞음"
            row["choices"][0]["text"] = "caller mutation"
        all_items = repo.items(case["user"], case["quiz"])
        all_items[0]["choices"][0]["text"] = "another caller mutation"
        assert repo.item(case["user"], case["quiz"], case["choice_item"])["choices"][0]["text"] == "맞음"
        assert len(whole_item_reads) == 1
    assert repo._CHECKED_ITEMS_KEY not in conn.info
    repo.item(case["user"], case["quiz"], case["choice_item"])
    assert len(whole_item_reads) == 2


@pytest.mark.parametrize("boundary", ["commit", "rollback", "different_user", "different_quiz"])
def test_checked_items_never_cross_transaction_or_ownership_boundary(
        domain_connection, answer_case, whole_item_reads, boundary):
    conn, case = domain_connection, answer_case
    other = make_case(conn)
    repo = QuizAnswerRepository(conn)
    with repo.checked_items(case["user"], case["quiz"]):
        if boundary in ("commit", "rollback"):
            getattr(conn, boundary)()
        else:
            user = other["user"] if boundary == "different_user" else case["user"]
            quiz = case["quiz"] if boundary == "different_user" else other["quiz"]
            with pytest.raises(ApiError) as denied:
                repo.item(user, quiz, case["choice_item"])
            assert denied.value.code == "NO_QUIZ"
        conn.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(
            choices=[{"text": "새 보기"}]))
        assert repo.item(case["user"], case["quiz"], case["choice_item"])["choices"] == [{"text": "새 보기"}]
        assert repo._CHECKED_ITEMS_KEY not in conn.info
    assert len(whole_item_reads) == 2


def test_scope_exception_and_connection_close_clear_physical_connection_state(domain_connection, answer_case):
    conn, case = domain_connection, answer_case
    repo = QuizAnswerRepository(conn)
    with pytest.raises(RuntimeError):
        with repo.checked_items(case["user"], case["quiz"]):
            raise RuntimeError("rollback path")
    assert repo._CHECKED_ITEMS_KEY not in conn.info
    conn.commit()
    with conn.engine.connect() as separate:
        if separate.dialect.name == "postgresql":
            from sqlalchemy import text
            schema = conn.scalar(text("select current_schema()"))
            separate.exec_driver_sql(f'SET search_path TO "{schema}"')
        info = separate.info
        with QuizAnswerRepository(separate).checked_items(case["user"], case["quiz"]):
            separate.close()
        assert repo._CHECKED_ITEMS_KEY not in info


def test_checked_scope_is_absent_during_external_grading(api_client, answer_case, monkeypatch):
    from tests.test_quiz_finalize import submit, use_gateway
    observed = []
    def outside(identity):
        from app.db.session import engine
        assert engine.pool.checkedout() == 0
        with engine.begin() as connection:
            assert QuizAnswerRepository._CHECKED_ITEMS_KEY not in connection.info
            QuizAnswerRepository(connection).lock_quiz(answer_case["user"], answer_case["quiz"])
        observed.append(True)
    use_gateway(monkeypatch, hook=outside, score=85)
    response = submit(api_client, answer_case, text="실제 채점 경계")
    assert response.status_code == 200, response.text
    assert observed == [True]
