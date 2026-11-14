"""Durable PDF worker: python -m app.workers.materials [--loop]."""
import argparse
import signal
import threading


def main(argv=None):
    parser = argparse.ArgumentParser(description='Process durable PDF analysis jobs')
    parser.add_argument('--loop', action='store_true')
    args = parser.parse_args(argv)
    from app.db.session import check_db_revision, engine
    from app.services.material_analysis import MaterialAnalysisService
    check_db_revision()
    service, stop = MaterialAnalysisService(engine), threading.Event()
    previous = {}
    for number in (signal.SIGINT, signal.SIGTERM):
        previous[number] = signal.signal(number, lambda *_: stop.set())
    try:
        while not stop.is_set():
            service.run_next()
            if not args.loop:
                break
            stop.wait(1)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
