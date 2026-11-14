from dataclasses import replace

import pytest
from sqlalchemy import select

from app.ai.gateway import GatewayRequest, InvalidOutput, MaterialRevision
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.user import User
from app.repositories.users import UserRepository
from tests.test_ai_gateway import NOW, add_material, gateway_user, identity, result_repository


@pytest.mark.parametrize("rollback", [False, True])
def test_seed_result_uses_caller_transaction_without_an_early_commit(
        result_repository, domain_connection, rollback):
    conn = domain_connection
    user = User.anonymous(now=NOW)
    UserRepository(conn).add(user)
    request = GatewayRequest(user.id, identity())
    value = {"examDate": "2026-12-01"}
    result_id = result_repository.save_in_transaction(conn, request, value)
    value["examDate"] = "changed outside the stored result"
    assert conn.in_transaction() and not conn.closed
    assert conn.scalar(select(t.ai_saved_results.c.result).where(t.ai_saved_results.c.id == result_id)) == {
        "examDate": "2026-12-01"}
    with result_repository.engine.connect() as other:
        assert other.scalar(select(t.users.c.id).where(t.users.c.id == user.id)) is None
        assert other.scalar(select(t.ai_saved_results.c.id).where(t.ai_saved_results.c.id == result_id)) is None
    if rollback:
        conn.rollback()
        assert conn.scalar(select(t.users.c.id).where(t.users.c.id == user.id)) is None
        assert conn.scalar(select(t.ai_saved_results.c.id).where(t.ai_saved_results.c.id == result_id)) is None
    else:
        conn.commit()
        saved = result_repository.lookup(request)
        assert saved.id == result_id and saved.source == "private_cache"
        assert saved.result == {"examDate": "2026-12-01"}


def test_transactional_seed_result_refuses_foreign_material_and_invalid_json(
        result_repository, domain_connection, gateway_user):
    conn = domain_connection
    material_id = add_material(conn, gateway_user)
    other = User.anonymous(now=NOW)
    UserRepository(conn).add(other)
    conn.commit()
    request = GatewayRequest(other.id, identity(materials=[MaterialRevision(material_id, "v1")]))
    with pytest.raises(ApiError) as error:
        result_repository.save_in_transaction(conn, request, {"score": 70})
    assert error.value.status == 404 and error.value.code == "NO_MATERIAL"
    with pytest.raises(InvalidOutput):
        result_repository.save_in_transaction(conn, replace(request, owner_id=gateway_user), {"score": float("nan")})
    assert conn.execute(select(t.ai_saved_results)).first() is None
