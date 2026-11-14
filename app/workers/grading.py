"""Run with python -m app.workers.grading --once or --loop."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
from uuid import UUID, uuid4

from app.ai.gateway import CleanupFailed
from app.ai.settings import AiSettings
from app.services.grading import GradingService


def _positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Use positive finite poll seconds")
    return number


def _parser():
    parser = argparse.ArgumentParser(description="Durable quiz grading worker")
    run = parser.add_mutually_exclusive_group()
    run.add_argument("--once", action="store_true", help="Process at most one eligible job and exit")
    run.add_argument("--loop", action="store_true", help="Poll until SIGTERM or SIGINT")
    parser.add_argument("--retry-job", type=UUID, help="Explicitly retry this job, including a failed job")
    parser.add_argument("--user-id", type=UUID, help="Owner required for an explicit retry")
    parser.add_argument("--retry-mode", choices=("live", "saved", "backup"), help="Explicit retry execution mode")
    parser.add_argument("--poll-seconds", type=_positive, default=1.0)
    parser.add_argument("--health-file", type=Path, default=Path("/tmp/grading-worker-health.json"))
    parser.add_argument("--healthcheck", action="store_true", help="Check the loop process heartbeat without DB/AI initialization")
    return parser


def healthy(path):
    try:
        value = json.loads(path.read_text())
        if type(value["pid"]) is not int or value["pid"] <= 0 or time.time() >= value["expires_at"]:
            return False
        os.kill(value["pid"], 0)
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _heartbeat(path, lifespan):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "expires_at": time.time() + lifespan}))
    temporary.replace(path)


def _clear_heartbeat(path):
    try:
        if json.loads(path.read_text()).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def main(argv=None, *, service_factory=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.healthcheck:
        return 0 if healthy(args.health_file) else 1
    if bool(args.retry_job) != bool(args.user_id) or (args.retry_job and args.loop) or (args.retry_mode and not args.retry_job):
        parser.error("Explicit retries require --retry-job and --user-id, and cannot run in --loop mode")
    stop = threading.Event()
    previous = {}
    def stopping(signum, frame):
        # Finish the current bounded gateway execution so its process is reaped
        # and already observed usage is committed. Never start another job.
        stop.set()
    for number in (signal.SIGTERM, signal.SIGINT):
        previous[number] = signal.signal(number, stopping)
    try:
        from app.db.session import check_db_revision, engine
        check_db_revision()
        settings = AiSettings.from_environment()
        worker = (service_factory or GradingService)(engine, owner="grading-" + str(uuid4()), settings=settings)
        health_lifespan = max(30, settings.request_budget_seconds + args.poll_seconds + 10)
        while not stop.is_set():
            if args.loop:
                _heartbeat(args.health_file, health_lifespan)
            if args.retry_job:
                outcome = worker.run_job(str(args.user_id), str(args.retry_job), retry_failed=True, mode=args.retry_mode)
            else:
                outcome = worker.run_next(stop_requested=stop.is_set)
            if outcome.state != "idle":
                print(json.dumps({"jobId": outcome.job_id, "state": outcome.state, "errorCode": outcome.error_code}), flush=True)
            if not args.loop:
                return 0
            stop.wait(args.poll_seconds)
        return 0
    except CleanupFailed:
        print("Fatal grading worker error: CleanupFailed", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        # No raw SDK/DB exception body: it may contain credentials or answer text.
        print("Fatal grading worker error: " + type(exc).__name__, file=sys.stderr, flush=True)
        return 1
    finally:
        if args.loop:
            _clear_heartbeat(args.health_file)
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
