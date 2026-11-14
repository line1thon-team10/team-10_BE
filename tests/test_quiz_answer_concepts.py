from uuid import uuid4

import pytest
from sqlalchemy import update

from app.db import schema as t
from tests.test_quiz_answer_api import NOW, answer_case, make_case, put, snapshot


@pytest.mark.parametrize("link", ["own-unit", "other-owned-unit", "foreign-unit"])
@pytest.mark.parametrize("method", ["put", "get"])
@pytest.mark.parametrize("already_saved", [False, True])
def test_concept_must_belong_to_question_unit_before_save_restore_or_replay(
        api_client, answer_case, domain_connection, link, method, already_saved):
    case, conn = answer_case, domain_connection
    if already_saved:
        assert put(api_client, case).status_code == 200
    other = make_case(conn)
    if link == "other-owned-unit":
        conn.execute(update(t.subjects).where(t.subjects.c.id == other["subject"])
                     .values(user_id=case["user"], code="OTHER"))
    concept = str(uuid4())
    conn.execute(t.concepts.insert().values(id=concept,
        unit_id=case["unit"] if link == "own-unit" else other["unit"], code="C1", title="개념",
        sort_order=1, start_page=1, end_page=2, created_at=NOW, updated_at=NOW))
    conn.execute(update(t.questions).where(t.questions.c.id == case["essay"]).values(concept_id=concept))
    before = snapshot(conn)
    conn.commit()
    response = put(api_client, case) if method == "put" else api_client.get(
        f"/api/quiz/{case['quiz']}", headers={"X-Anonymous-Id": case["user"]})
    if link == "own-unit":
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "NO_QUIZ"
        assert snapshot(conn) == before
