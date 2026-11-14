"""Durable PDF worker: python -m app.workers.materials [--loop]."""
import argparse
from pathlib import Path
import signal
import sys
import threading

from app.workers.grading import _clear_heartbeat, _heartbeat, healthy


def main(argv=None, *, service_factory=None):
    parser = argparse.ArgumentParser(description='Process durable PDF analysis jobs')
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--health-file', type=Path, default=Path('/tmp/material-worker-health.json'))
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args(argv)
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
        service = (service_factory or MaterialAnalysisService)(engine)
        lifespan = AiSettings.from_environment().pdf_job_budget_seconds + 15
        while not stop.is_set():
            if args.loop:
                _heartbeat(args.health_file, lifespan)
            service.run_next()
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
