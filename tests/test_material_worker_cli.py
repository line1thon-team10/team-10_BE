import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_healthcheck_does_not_initialize_database(tmp_path):
    result = subprocess.run([sys.executable,'-m','app.workers.materials','--healthcheck','--health-file',str(tmp_path/'missing')],
        cwd=ROOT, env={**os.environ,'DATABASE_URL':'invalid'}, capture_output=True,text=True,timeout=10)
    assert result.returncode == 1 and result.stderr == ''


@pytest.mark.parametrize('shutdown', [signal.SIGTERM, signal.SIGINT])
def test_material_cli_drains_current_job_and_clears_heartbeat(tmp_path, shutdown):
    marker, health = tmp_path/'started', tmp_path/'health.json'
    source = '''
from pathlib import Path
import time
from app.db import session
session.check_db_revision = lambda: None
from app.workers.materials import main
class Worker:
    def __init__(self,engine): pass
    def run_next(self):
        with Path(__import__('sys').argv[1]).open('a') as out: out.write('start\\n')
        time.sleep(1)
        with Path(__import__('sys').argv[1]).open('a') as out: out.write('finish\\n')
raise SystemExit(main(['--loop','--health-file',__import__('sys').argv[2]],service_factory=Worker))
'''
    process = subprocess.Popen([sys.executable,'-c',source,str(marker),str(health)],cwd=ROOT,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        until = time.monotonic()+8
        while not marker.exists() and process.poll() is None and time.monotonic()<until:
            time.sleep(.01)
        assert marker.exists()
        assert json.loads(health.read_text())['expires_at'] > time.time()
        process.send_signal(shutdown)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert marker.read_text() == 'start\nfinish\n'
        assert not health.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_material_cli_failure_never_logs_provider_body():
    source = '''
from app.db import session
session.check_db_revision = lambda: None
from app.ai.gateway import CleanupFailed
from app.workers.materials import main
class Worker:
    def __init__(self,engine): pass
    def run_next(self): raise CleanupFailed('secret-api-key')
raise SystemExit(main([],service_factory=Worker))
'''
    result = subprocess.run([sys.executable,'-c',source],cwd=ROOT,capture_output=True,text=True,timeout=10)
    assert result.returncode == 1 and 'CleanupFailed' in result.stderr
    assert 'secret-api-key' not in result.stdout+result.stderr
