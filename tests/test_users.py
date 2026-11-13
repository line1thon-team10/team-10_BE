from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.user import User
from app.repositories.users import UserRepository


def new_user():
    return User.anonymous(now=datetime(2026, 11, 13, 9, tzinfo=timezone(timedelta(hours=9))))


def test_new_user_has_unique_uuid_and_empty_initial_state():
    first, second = new_user(), new_user()
    assert UUID(first.id).version == 4
    assert first.id != second.id
    assert first.timezone == "Asia/Seoul"
    assert first.onboarding_status == "draft"
    assert first.demo_date is None and first.last_assessed_date is None
    assert first.created_at == first.updated_at == datetime(2026, 11, 13, tzinfo=timezone.utc)
    assert first.id not in repr(first)


def test_creation_rejects_ambiguous_naive_timestamp():
    with pytest.raises(ValueError, match="timezone"):
        User.anonymous(now=datetime(2026, 11, 13))


def test_repository_persists_and_reads_only_requested_user(domain_connection):
    repo = UserRepository(domain_connection)
    first, second = new_user(), new_user()
    repo.add(first)
    repo.add(second)
    domain_connection.commit()
    assert repo.get(UUID(first.id)) == first
    assert repo.get(UUID(second.id)) == second
    assert repo.get(uuid4()) is None


def test_repository_leaves_transaction_control_to_caller(domain_connection):
    repo = UserRepository(domain_connection)
    user = new_user()
    repo.add(user)
    assert repo.get(UUID(user.id)) == user
    domain_connection.rollback()
    assert repo.get(UUID(user.id)) is None


def test_duplicate_id_cannot_replace_existing_user(domain_connection):
    repo = UserRepository(domain_connection)
    user = new_user()
    repo.add(user)
    domain_connection.commit()
    with pytest.raises(IntegrityError):
        repo.add(user)
    domain_connection.rollback()
    assert repo.get(UUID(user.id)) == user
