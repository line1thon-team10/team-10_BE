from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.core.clock import now_utc
from app.db import schema as t


@pytest.fixture
def unit_case(api_client, domain_connection):
    conn, now = domain_connection, now_utc()
    ids = {key: str(uuid4()) for key in (
        "user", "other_user", "subject", "second_subject", "other_subject", "material", "second_material", "other_material")}
    for key in ("user", "other_user"):
        conn.execute(t.users.insert().values(id=ids[key], created_at=now, updated_at=now))
    for key, owner in (("subject", "user"), ("second_subject", "user"), ("other_subject", "other_user")):
        conn.execute(t.subjects.insert().values(id=ids[key], user_id=ids[owner], code=key, name=key,
            color="#123456", status="active", created_at=now, updated_at=now))
    for key, subject in (("material", "subject"), ("second_material", "subject"), ("other_material", "other_subject")):
        conn.execute(t.materials.insert().values(id=ids[key], subject_id=ids[subject], original_name=f"{key}.pdf",
            source_format="pdf", sha256=key, analysis_version="test", page_count=30,
            analysis_status="succeeded", quiz_enabled=key != "second_material", created_at=now, updated_at=now))
    conn.commit()
    return {**ids, "headers": {"X-Anonymous-Id": ids["user"]}}


def add_unit(conn, case, *, material="material", no=1, start=1, end=12, order=1, **values):
    identifier, now = str(uuid4()), now_utc()
    row = dict(id=identifier, material_id=case[material], code=f"unit{no}", title=f"단원 {no}", sort_order=order,
        quantity_basis="page", start_page=start, end_page=end, difficulty="mid", in_scope=True,
        evidence=["PDF에서 확인한 목차"], created_at=now, updated_at=now)
    conn.execute(t.units.insert().values(**(row | values)))
    return identifier


def add_concept(conn, unit, *, start=1, end=5, order=1, **values):
    identifier, now = str(uuid4()), now_utc()
    row = dict(id=identifier, unit_id=unit, code=f"concept{order}", title=f"개념 {order}", sort_order=order,
        start_page=start, end_page=end, in_scope=True, created_at=now, updated_at=now)
    conn.execute(t.concepts.insert().values(**(row | values)))
    return identifier


def get_units(client, case, **params):
    return client.get("/api/units", headers=case["headers"], params={
        "subjectId": case["subject"], "materialId": case["material"], **params})


def test_get_material_local_structure_preserves_ids_order_and_server_evidence(api_client, domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    second = add_unit(conn, case, no=8, start=13, end=30, order=2)
    first = add_unit(conn, case, no=4, order=1, difficulty=None, in_scope=False)
    later = add_concept(conn, first, start=4, end=9, order=2, in_scope=False)
    earlier = add_concept(conn, first, order=1)
    retired = add_unit(conn, case, retired_at=now_utc())
    add_concept(conn, retired)
    add_concept(conn, first, retired_at=now_utc())
    other_pdf = add_unit(conn, case, material="second_material", no=4)
    add_unit(conn, case, material="other_material")
    before = [dict(row) for row in conn.execute(select(t.units)).mappings()]
    conn.commit()
    response = get_units(api_client, case, subjectId=UUID(case["subject"]).hex.upper())
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()["data"]
    assert data["file"] == {"id": case["material"], "name": "material.pdf", "pages": 30}
    assert data["subjectId"] == case["subject"] and data["materialId"] == case["material"]
    assert [row["id"] for row in data["units"]] == [first, second]
    assert data["units"][0] == {"id": first, "no": 4, "title": "단원 4", "from": 1, "to": 12,
        "difficulty": None, "inExam": False, "quizless": False, "evidence": ["PDF에서 확인한 목차"],
        "concepts": [{"id": earlier, "title": "개념 1", "from": 1, "to": 5, "inExam": True},
                     {"id": later, "title": "개념 2", "from": 4, "to": 9, "inExam": False}]}
    assert data["units"][1]["concepts"] == []
    second_data = get_units(api_client, case, materialId=case["second_material"]).json()["data"]
    assert second_data["units"][0]["id"] == other_pdf and second_data["units"][0]["quizless"]
    assert [dict(row) for row in conn.execute(select(t.units)).mappings()] == before


def test_get_empty_structure_does_not_invent_pages_or_evidence(api_client, domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    conn.execute(update(t.materials).where(t.materials.c.id == case["material"]).values(page_count=None))
    conn.commit()
    response = get_units(api_client, case)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["units"] == [] and response.json()["data"]["file"]["pages"] is None


@pytest.mark.parametrize("which", ["missing", "foreign", "wrong_subject"])
def test_get_checks_material_ownership_and_subject_membership(api_client, unit_case, which):
    params = {"missing": {"materialId": str(uuid4())}, "foreign": {"materialId": unit_case["other_material"]},
              "wrong_subject": {"subjectId": unit_case["second_subject"]}}[which]
    response = get_units(api_client, unit_case, **params)
    assert response.status_code == (422 if which == "wrong_subject" else 404)
    assert response.json()["error"]["code"] == ("INVALID_INPUT" if which == "wrong_subject" else "NO_MATERIAL")


@pytest.mark.parametrize("params", [{}, {"subjectId": "bad", "materialId": "bad"},
    {"subjectId": str(uuid4())}, {"materialId": str(uuid4())},
    {"subjectId": str(uuid4()), "materialId": str(uuid4()), "unknown": "x"}])
def test_get_requires_both_uuid_parameters_and_rejects_extras(api_client, unit_case, params):
    response = api_client.get("/api/units", headers=unit_case["headers"], params=params)
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_INPUT"


def test_get_requires_anonymous_session(api_client, unit_case):
    response = api_client.get("/api/units", params={"subjectId": unit_case["subject"], "materialId": unit_case["material"]})
    assert response.status_code == 401 and response.json()["error"]["code"] == "NO_SESSION"
