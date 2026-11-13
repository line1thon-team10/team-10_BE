import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/line1thon-team10/2026-line1-be@sha256:' + 'a' * 64


@pytest.fixture
def deployment(tmp_path):
    shutil.copy(ROOT / 'deploy/apply.sh', tmp_path / 'apply.sh')
    binary = tmp_path / 'bin'
    binary.mkdir()
    docker = binary / 'docker'
    docker.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DEPLOY_LOG"
if [[ -n "${FAIL_MATCH:-}" && "$*" == *"$FAIL_MATCH"* ]]; then exit 1; fi
if [[ "$*" == *"pg_dump"* ]]; then echo '-- backup'; fi
''')
    docker.chmod(0o755)
    flock = binary / 'flock'
    flock.write_text('#!/usr/bin/env bash\nexit "${LOCK_FAILURE:-0}"\n')
    flock.chmod(0o755)
    env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
           'DEPLOY_LOG': str(tmp_path / 'docker.log')}
    return tmp_path, env


def run(deployment, image=IMAGE, **changes):
    directory, env = deployment
    result = subprocess.run(['bash', str(directory / 'apply.sh'), image],
                            env={**env, **changes}, capture_output=True, text=True)
    log = directory / 'docker.log'
    return result, log.read_text() if log.exists() else ''


@pytest.mark.parametrize('image', ['latest', 'ghcr.io/other/repo@sha256:' + 'a' * 64,
                                  IMAGE + '; echo bad', IMAGE[:-1]])
def test_only_project_digest_is_accepted(deployment, image):
    result, log = run(deployment, image)
    assert result.returncode == 2
    assert log == ''


def test_concurrent_deployment_stops_before_docker(deployment):
    result, log = run(deployment, LOCK_FAILURE='1')
    assert result.returncode != 0
    assert log == ''


def test_pull_failure_keeps_running_service(deployment):
    result, log = run(deployment, FAIL_MATCH=' pull')
    assert result.returncode != 0
    assert 'stop api' not in log


@pytest.mark.parametrize('failure', ['pg_dump', 'run --rm --no-deps migrate', 'up -d --no-deps --wait'])
def test_failure_does_not_record_new_image(deployment, failure):
    directory, _ = deployment
    (directory / 'deployed-image.txt').write_text('previous-image\n')
    result, log = run(deployment, FAIL_MATCH=failure)
    assert result.returncode != 0
    assert (directory / 'deployed-image.txt').read_text() == 'previous-image\n'
    if failure != 'up -d --no-deps --wait':
        assert 'up -d --no-deps --wait' not in log


def test_backup_precedes_migration_and_success_records_digest(deployment):
    directory, _ = deployment
    result, log = run(deployment)
    assert result.returncode == 0, result.stderr
    assert log.index('stop api') < log.index('pg_dump') < log.index('run --rm --no-deps migrate')
    assert log.index('run --rm --no-deps migrate') < log.index('up -d --no-deps --wait')
    assert (directory / 'deployed-image.txt').read_text().strip() == IMAGE
    assert len(list((directory / 'backups').glob('*.sql'))) == 1
