from sqlalchemy import Connection, select
from sqlalchemy.sql.dml import Update

from app.db import schema as t
from app.repositories.quiz_answers import QuizAnswerRepository
from tests.test_quiz_answer_api import answer_case
from tests.test_quiz_finalize import facts, submit


def test_retry_cas_loss_rolls_back_entire_input_and_seal_transaction(
        api_client, answer_case, domain_connection, monkeypatch):
    original = Connection.execute
    def miss(connection, statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == "quiz_grading_jobs":
            statement = statement.where(t.quiz_grading_jobs.c.id == "00000000-0000-0000-0000-000000000001")
        return original(connection, statement, *args, **kwargs)
    monkeypatch.setattr(Connection, "execute", miss)
    response = submit(api_client, answer_case)
    assert response.status_code == 409 and response.json()["error"]["code"] == "ANSWER_CONFLICT"
    for table in (t.quiz_answers, t.quiz_answer_heads, t.quiz_answer_receipts, t.quiz_grading_jobs, t.quiz_submission_states):
        assert domain_connection.execute(select(table)).first() is None


def test_competing_first_snapshot_never_commits_this_requests_score_writes(
        api_client, answer_case, domain_connection, monkeypatch):
    original = QuizAnswerRepository.store_result_snapshot
    def competing(self, user_id, quiz_id, *, snapshot, **kwargs):
        return original(self, user_id, quiz_id, snapshot={"other": "first response"}, **kwargs)
    monkeypatch.setattr(QuizAnswerRepository, "store_result_snapshot", competing)
    response = submit(api_client, answer_case)
    assert response.status_code == 409 and response.json()["error"]["code"] == "ANSWER_CONFLICT"
    assert facts(domain_connection, answer_case) == {"attempts": [], "history": []}
    assert domain_connection.execute(select(t.weak_questions)).first() is None
    state = domain_connection.execute(select(t.quiz_submission_states)).mappings().one()
    assert state["result_snapshot"] is None and len(state["sealed_answers"]) == 4
    domain_connection.commit()
    monkeypatch.setattr(QuizAnswerRepository, "store_result_snapshot", original)
    assert submit(api_client, answer_case).status_code == 200
