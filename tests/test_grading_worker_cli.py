"""Actual CLI subprocess lifecycle with local DB and synthetic providers only."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from sqlalchemy import select, text, update

from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.workers.grading import healthy
from test_grading_worker import worker_case, worker_engine, queue, job_row


ROOT = Path(__file__).resolve().parents[1]


def cli_environment(engine, connection, **changes):
    url = engine.url
    if engine.dialect.name == "postgresql":
        schema = connection.scalar(text("select current_schema()"))
        url = url.update_query_dict({"options": f"-csearch_path={schema}"})
        connection.commit()
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False), "AI_REQUEST_BUDGET_SECONDS": "1.5",
           "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT / "tests"), **changes}
    env.pop("GROQ_API_KEY", None)
    return env


def test_actual_cli_once_processes_a_deterministic_job_without_ai_keys(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, kind="choice")
    result = subprocess.run([sys.executable, "-m", "app.workers.grading", "--once"], cwd=ROOT,
        env=cli_environment(worker_engine, conn), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "succeeded"
    assert job_row(conn, job)["result"]["is_correct"] is True
    assert conn.execute(select(t.quiz_attempts)).first() is None


def forbidden_provider(call, context):
    Path(os.environ["GRADING_TEST_FORBIDDEN_CALL"]).write_text("provider was invoked")
    raise RuntimeError("Saved execution must never reach a provider")


@pytest.mark.parametrize("mode", ["saved", "backup"])
def test_actual_cli_restart_preserves_expired_saved_mode_and_does_not_retry_failure(
    domain_connection, worker_engine, worker_case, tmp_path, mode
):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, mode=mode)
    abandoned = GradingJobRepository(conn).claim(case["user"], job["id"], owner="dead-process",
        now=datetime.now(timezone.utc) - timedelta(minutes=1), lease_for=timedelta(seconds=1))
    assert abandoned is not None
    conn.commit()
    marker = tmp_path / "forbidden-provider"
    bootstrap = """
from app.ai.gateway import FunctionRef, ProviderSpec
from app.services.grading import GradingService
from app.workers.grading import main
provider = ProviderSpec(FunctionRef('test_grading_worker_cli', 'forbidden_provider'))
def factory(engine, **kwargs):
    return GradingService(engine, provider=provider, **kwargs)
raise SystemExit(main(['--once'], service_factory=factory))
"""
    env = cli_environment(worker_engine, conn, GRADING_TEST_FORBIDDEN_CALL=str(marker))
    first = subprocess.run([sys.executable, "-c", bootstrap], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=10)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["state"] == "failed"
    row = job_row(conn, job)
    assert row["execution_mode"] == mode and row["claim_count"] == 2
    assert row["status"] == "failed" and row["result"] is None
    assert row["lease_token"] != abandoned["lease_token"]
    assert not marker.exists() and conn.execute(select(t.ai_usage_logs)).first() is None
    conn.rollback()
    second = subprocess.run([sys.executable, "-c", bootstrap], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=10)
    assert second.returncode == 0 and second.stdout == "", second.stderr
    assert job_row(conn, job)["claim_count"] == 2
    assert not marker.exists() and conn.execute(select(t.ai_usage_logs)).first() is None


def hanging_provider(call, context):
    context.start_request(provider="fake", model="hanging")
    Path(os.environ["GRADING_TEST_CHILD_PID"]).write_text(str(os.getpid()))
    time.sleep(60)


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_claiming_and_reaps_current_provider_before_cli_exit(domain_connection, worker_engine, worker_case, tmp_path, stop_signal):
    conn, case = domain_connection, worker_case
    running = queue(conn, case)
    waiting = queue(conn, case, kind="choice")
    conn.execute(update(t.quiz_grading_jobs).where(t.quiz_grading_jobs.c.id == running["id"]).values(
        created_at=running["created_at"].replace(year=2025)))
    conn.commit()
    child_path, health_path = tmp_path / "child.pid", tmp_path / "health.json"
    bootstrap = """
from app.ai.gateway import FunctionRef, ProviderSpec
from app.services.grading import GradingService
from app.workers.grading import main
provider = ProviderSpec(FunctionRef('test_grading_worker_cli', 'hanging_provider'))
def factory(engine, **kwargs):
    return GradingService(engine, provider=provider, **kwargs)
raise SystemExit(main(['--loop', '--poll-seconds', '0.05', '--health-file', __import__('sys').argv[1]], service_factory=factory))
"""
    process = subprocess.Popen([sys.executable, "-c", bootstrap, str(health_path)], cwd=ROOT,
        env=cli_environment(worker_engine, conn, GRADING_TEST_CHILD_PID=str(child_path)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        until = time.monotonic() + 8
        while not child_path.exists() and process.poll() is None and time.monotonic() < until:
            time.sleep(0.01)
        assert child_path.exists()
        child = int(child_path.read_text())
        assert healthy(health_path)
        process.send_signal(stop_signal)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stderr
        assert "failed" in stdout and not health_path.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        assert job_row(conn, running)["status"] == "failed"
        assert job_row(conn, waiting)["status"] == "queued" and job_row(conn, waiting)["claim_count"] == 0
        usage = conn.execute(select(t.ai_usage_logs)).mappings().one()
        assert usage["status"] == "failed" and usage["job_id"] is None
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_cleanup_failed_makes_actual_cli_process_fail_without_sensitive_exception_text(domain_connection, worker_engine):
    bootstrap = """
from app.ai.gateway import CleanupFailed
from app.workers.grading import main
class BrokenWorker:
    def __init__(self, *args, **kwargs): pass
    def run_next(self, **kwargs): raise CleanupFailed('secret-api-key must not enter logs')
raise SystemExit(main(['--once'], service_factory=BrokenWorker))
"""
    result = subprocess.run([sys.executable, "-c", bootstrap], cwd=ROOT, env=cli_environment(worker_engine, domain_connection),
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and "CleanupFailed" in result.stderr
    assert "secret-api-key" not in result.stderr + result.stdout


def test_healthcheck_missing_file_does_not_initialize_database_or_provider(tmp_path):
    env = {**os.environ, "DATABASE_URL": "an-invalid-url", "GROQ_API_KEY": "do-not-use"}
    result = subprocess.run([sys.executable, "-m", "app.workers.grading", "--healthcheck", "--health-file", str(tmp_path / "absent")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and result.stderr == ""


@pytest.mark.parametrize("arguments", [["--poll-seconds", "nan"], ["--poll-seconds", "0"],
    ["--retry-job", "bad-id"], ["--retry-mode", "saved"], ["--user-id", "00000000-0000-0000-0000-000000000001"]])
def test_cli_rejects_invalid_operational_inputs_before_db_initialization(arguments):
    result = subprocess.run([sys.executable, "-m", "app.workers.grading", *arguments], cwd=ROOT,
        env={**os.environ, "DATABASE_URL": "an-invalid-url"}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "usage:" in result.stderr
