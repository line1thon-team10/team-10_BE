"""Independent long-job heartbeat and older-image deployment compatibility."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_heartbeat_covers_configured_long_pdf_job_and_is_removed_after_drain(tmp_path):
    script = '''
import json
from pathlib import Path
import os
import signal
import sys
import time
from app.db import session
from app.workers.materials import main
session.check_db_revision = lambda: None
health = Path(sys.argv[1])
class Worker:
    def __init__(self, engine): pass
    def run_next(self, **kwargs):
        value = json.loads(health.read_text())
        assert value['pid'] == os.getpid()
        assert value['expires_at'] - time.time() >= 900
        os.kill(os.getpid(), signal.SIGTERM)
        return True
result = main(['--loop', '--health-file', str(health)], service_factory=Worker)
assert result == 0 and not health.exists()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'health.json')], cwd=ROOT,
        env={**os.environ, 'AI_PDF_JOB_BUDGET_SECONDS': '900'}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr



def test_existing_grading_image_without_material_worker_starts_only_supported_services(tmp_path):
    import shutil
    from tests.test_deployment import IMAGE
    directory = tmp_path
    shutil.copy(ROOT / 'deploy/apply.sh', directory / 'apply.sh')
    binary = directory / 'bin'
    binary.mkdir()
    docker = binary / 'docker'
    docker.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DEPLOY_LOG"
if [[ "$*" == *"config --services"* ]]; then printf '%s\\n' db migrate api grading-worker material-worker; fi
if [[ "$*" == "run --rm --entrypoint python "* && "$*" == *"app.workers.materials"* ]]; then exit 42; fi
if [[ "$*" == *"pg_dump"* ]]; then echo '-- local stub backup'; fi
if [[ "$*" == "ps --filter "* ]]; then
  printf '%s\\n' '111111111111|line1-2026|material-worker|' '222222222222|line1-2026|custom-worker|true' '333333333333|other-project|material-worker|true'
fi
''')
    docker.chmod(0o755)
    flock = binary / 'flock'
    flock.write_text('#!/usr/bin/env bash\nexit 0\n')
    flock.chmod(0o755)
    log = directory / 'docker.log'
    result = subprocess.run(['bash', str(directory / 'apply.sh'), IMAGE], cwd=directory,
        env={**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'], 'DEPLOY_LOG': str(log)},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    stopped = next(line for line in calls.splitlines() if line.startswith('stop --time'))
    assert stopped.endswith('111111111111 222222222222') and '333333333333' not in stopped
    assert calls.index(stopped) < calls.index('pg_dump') < calls.index('run --rm --no-deps migrate')
    start = next(line for line in calls.splitlines() if 'up -d --no-deps --wait ' in line)
    assert start.endswith('api grading-worker') and 'material-worker' not in start

