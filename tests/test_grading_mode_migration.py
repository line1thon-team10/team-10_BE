from datetime import timedelta
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db import schema as t
from app.db.migrations import migration_config
from app.repositories.quiz_answers import QuizAnswerRepository
from test_quiz_storage import NOW, make_case, save


def old_jobs(connection):
    table = sa.Table("quiz_grading_jobs", sa.MetaData(), autoload_with=connection)
    for name in ("id", "answer_id", "lease_token"):
        table.c[name].type = sa.Uuid(as_uuid=False)
    return table


def job_values(answer_id, status="queued", **changes):
    return {"id": str(uuid4()), "answer_id": answer_id, "answer_revision": 1, "status": status, "claim_count": 0,
        "created_at": NOW, "result": {"is_correct": True, "essay_score": 85} if status == "succeeded" else sa.null(),
        "lease_owner": "old-worker" if status == "running" else None,
        "lease_token": str(uuid4()) if status == "running" else None,
        "lease_expires_at": NOW + timedelta(seconds=30) if status == "running" else None,
        "claimed_at": NOW if status == "running" else None, **changes}


def test_mode_upgrade_preserves_existing_jobs_answers_and_results_with_live_default(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0007_preview_storage")
    previous = old_jobs(conn)
    for status in ("queued", "running", "failed", "cancelled", "succeeded"):
        case = make_case(conn)
        answer = save(QuizAnswerRepository(conn), case).answer
        conn.execute(previous.insert().values(**job_values(answer["id"], status)))
    before = [dict(row) for row in conn.execute(sa.select(previous).order_by(previous.c.id)).mappings()]
    answers = [dict(row) for row in conn.execute(sa.select(t.quiz_answers).order_by(t.quiz_answers.c.id)).mappings()]
    conn.commit()
    for _ in range(2):
        command.upgrade(config, "head")
        after = [dict(row) for row in conn.execute(sa.select(t.quiz_grading_jobs).order_by(t.quiz_grading_jobs.c.id)).mappings()]
        assert all(row.pop("execution_mode") == "live" for row in after)
        assert after == before
        assert [dict(row) for row in conn.execute(sa.select(t.quiz_answers).order_by(t.quiz_answers.c.id)).mappings()] == answers
        assert compare_metadata(MigrationContext.configure(conn, opts={"compare_server_default": True}), t.metadata) == []
        assert MigrationContext.configure(conn).get_current_heads() == tuple(ScriptDirectory.from_config(config).get_heads())
        conn.commit()
        command.downgrade(config, "0007_preview_storage")
        assert "execution_mode" not in {column["name"] for column in sa.inspect(conn).get_columns("quiz_grading_jobs")}
        assert [dict(row) for row in conn.execute(sa.select(previous).order_by(previous.c.id)).mappings()] == before
        conn.commit()


def test_database_default_accepts_older_insert_that_omits_execution_mode(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0007_preview_storage")
    previous = old_jobs(conn)
    case = make_case(conn)
    answer = save(QuizAnswerRepository(conn), case).answer
    conn.commit()
    command.upgrade(config, "head")
    values = job_values(answer["id"])
    conn.execute(previous.insert().values(**values))
    assert conn.scalar(sa.select(t.quiz_grading_jobs.c.execution_mode).where(t.quiz_grading_jobs.c.id == values["id"])) == "live"


@pytest.mark.parametrize("mode", [None, "", "LIVE", "unknown"])
def test_execution_mode_is_nonnullable_and_closed_enum(domain_connection, mode):
    conn = domain_connection
    case = make_case(conn)
    answer = save(QuizAnswerRepository(conn), case).answer
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(t.quiz_grading_jobs.insert().values(**job_values(answer["id"], execution_mode=mode)))
    assert conn.execute(sa.select(t.quiz_grading_jobs)).first() is None


@pytest.mark.parametrize("mode", ["live", "saved", "backup"])
def test_execution_modes_survive_database_reopen(domain_connection, mode):
    conn = domain_connection
    case = make_case(conn)
    answer = save(QuizAnswerRepository(conn), case).answer
    values = job_values(answer["id"], execution_mode=mode)
    conn.execute(t.quiz_grading_jobs.insert().values(**values))
    conn.commit()
    assert conn.scalar(sa.select(t.quiz_grading_jobs.c.execution_mode).where(t.quiz_grading_jobs.c.id == values["id"])) == mode
