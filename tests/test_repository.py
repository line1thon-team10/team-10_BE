import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine
from app.repositories.base import Repository


@pytest.fixture
def repo():
    engine = create_engine('sqlite://')
    metadata = MetaData()
    table = Table('entries', metadata, Column('owner', String, primary_key=True),
                  Column('id', Integer, primary_key=True), Column('name', String, nullable=False))
    metadata.create_all(engine)
    class Entries(Repository):
        pass
    Entries.table = table
    with engine.begin() as conn:
        yield Entries(conn)
    engine.dispose()


def test_crud_and_owner_predicate(repo):
    key = {'owner': 'a', 'id': 1}
    repo.add({**key, 'name': 'first'})
    assert repo.get(key)['name'] == 'first'
    assert repo.get(key, repo.table.c.owner == 'b') is None
    assert repo.update(key, {'name': 'wrong'}, repo.table.c.owner == 'b') == 0
    assert repo.delete(key, repo.table.c.owner == 'b') == 0
    assert repo.update(key, {'name': 'next'}) == 1
    assert repo.list()[0]['name'] == 'next'
    assert repo.list(offset=1) == []
    assert repo.delete(key) == 1
    assert repo.get(key) is None
    assert repo.delete(key) == 0


@pytest.mark.parametrize('key', [{}, {'id': 1}, {'owner': None, 'id': 1}, {'owner': 'a', 'id': 1, 'extra': 2}])
def test_incomplete_keys_rejected(repo, key):
    with pytest.raises(ValueError):
        repo.delete(key)


@pytest.mark.parametrize('values', [{}, {'id': 2}])
def test_invalid_updates(repo, values):
    with pytest.raises(ValueError):
        repo.update({'owner': 'a', 'id': 1}, values)


@pytest.mark.parametrize('limit,offset', [(0,0), (1001,0), (1,-1)])
def test_pagination_bounds(repo, limit, offset):
    with pytest.raises(ValueError):
        repo.list(limit=limit, offset=offset)


def test_rollback(repo):
    transaction = repo.conn.begin_nested()
    repo.add({'owner': 'a', 'id': 1, 'name': 'rollback'})
    transaction.rollback()
    assert repo.list() == []
