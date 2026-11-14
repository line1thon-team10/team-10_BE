"""Durable PDF worker: python -m app.workers.materials [--loop]."""
import argparse
from pathlib import Path
import signal
import sys
import threading
from uuid import UUID

from app.workers.grading import _clear_heartbeat, _heartbeat, healthy


def main(argv=None, *, service_factory=None):
    parser = argparse.ArgumentParser(description='Process durable PDF analysis jobs')
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--health-file', type=Path, default=Path('/tmp/material-worker-health.json'))
    parser.add_argument('--healthcheck', action='store_true')
    parser.add_argument('--retry-bank-job', type=UUID, help='Requeue one failed question preparation job')
    parser.add_argument('--user-id', type=UUID, help='Owner of the failed question preparation job')
    args = parser.parse_args(argv)
    if bool(args.retry_bank_job) != bool(args.user_id):
        parser.error('--retry-bank-job and --user-id must be provided together')
    if args.retry_bank_job and (args.loop or args.healthcheck):
        parser.error('Bank retry admission cannot run with --loop or --healthcheck')
    if args.healthcheck:
        return 0 if healthy(args.health_file) else 1
    from app.db.session import check_db_revision, engine
    from app.services.material_analysis import MaterialAnalysisService
    from app.ai.settings import AiSettings
    stop, previous = threading.Event(), {}
    for number in (signal.SIGINT, signal.SIGTERM):
        previous[number] = signal.signal(number, lambda *_: stop.set())
    try:
        check_db_revision()
        if args.retry_bank_job:
            from app.services.question_bank import QuestionBankService
            queued = QuestionBankService(engine).retry(str(args.user_id), str(args.retry_bank_job))
            print('Bank retry queued' if queued else 'Bank job is not failed; no retry queued')
            return 0
        service = (service_factory or MaterialAnalysisService)(engine)
        lifespan = AiSettings.from_environment().pdf_job_budget_seconds + 15
        while not stop.is_set():
            if args.loop:
                _heartbeat(args.health_file, lifespan)
            service.run_next(stop_requested=stop.is_set)
            if not args.loop:
                break
            stop.wait(1)
        return 0
    except Exception as error:
        # SDK and database error bodies may contain source text or credentials.
        print('Fatal material worker error: ' + type(error).__name__, file=sys.stderr, flush=True)
        return 1
    finally:
        if args.loop:
            _clear_heartbeat(args.health_file)
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(main())
