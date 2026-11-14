import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/line1thon-team10/2026-line1-be@sha256:' + 'a' * 64
BE_REVISION = 'b' * 40
AI_REVISION = 'c' * 40


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
if [[ "$*" == *"config --services"* ]]; then printf '%s\\n' "$MOCK_SERVICES"; fi
if [[ "$*" == "run --rm --entrypoint python "* ]]; then exit "${WORKER_PROBE_STATUS:-0}"; fi
if [[ "$*" == "ps --filter "* ]]; then printf '%s\\n' "${MOCK_CONTAINER_ROWS:-}"; fi
if [[ "$*" == "image inspect "* ]]; then
  if [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
    printf '%s\\n' "$MOCK_BE_REVISION"
  elif [[ "$*" == *"dev.line1.ai-revision"* ]]; then
    printf '%s\\n' "$MOCK_AI_REVISION"
  fi
fi
''')
    docker.chmod(0o755)
    flock = binary / 'flock'
    flock.write_text('#!/usr/bin/env bash\nexit "${LOCK_FAILURE:-0}"\n')
    flock.chmod(0o755)
    env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
           'DEPLOY_LOG': str(tmp_path / 'docker.log'),
           'MOCK_BE_REVISION': BE_REVISION, 'MOCK_AI_REVISION': AI_REVISION,
           'MOCK_SERVICES': 'db\nmigrate\napi\ngrading-worker\nmaterial-worker'}
    return tmp_path, env


def run(deployment, image=IMAGE, revisions=(), **changes):
    directory, env = deployment
    result = subprocess.run(['bash', str(directory / 'apply.sh'), image, *revisions],
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


@pytest.mark.parametrize('revisions', [
    (BE_REVISION,), ('', AI_REVISION), (BE_REVISION[:-1], AI_REVISION),
    (BE_REVISION.upper(), AI_REVISION), (BE_REVISION, AI_REVISION + '; touch injected'),
])
def test_invalid_revision_manifest_stops_before_docker(deployment, revisions):
    result, log = run(deployment, revisions=revisions)
    assert result.returncode == 2
    assert log == ''


@pytest.mark.parametrize('changes', [
    {'MOCK_BE_REVISION': 'd' * 40}, {'MOCK_AI_REVISION': 'd' * 40},
    {'MOCK_BE_REVISION': ''}, {'MOCK_AI_REVISION': '<no value>'},
    {'FAIL_MATCH': 'image inspect'},
])
def test_image_revision_mismatch_keeps_existing_database_and_service(deployment, changes):
    directory, _ = deployment
    (directory / 'deployed-image.txt').write_text('previous-image\n')
    result, log = run(deployment, revisions=(BE_REVISION, AI_REVISION), **changes)
    assert result.returncode != 0
    assert ' pull' in log
    assert 'up -d --wait db' not in log
    assert 'stop api' not in log and 'pg_dump' not in log and ' migrate' not in log
    assert not (directory / 'backups').exists()
    assert (directory / 'deployed-image.txt').read_text() == 'previous-image\n'


def test_matching_image_is_verified_before_any_service_change(deployment):
    result, log = run(deployment, revisions=(BE_REVISION, AI_REVISION))
    assert result.returncode == 0, result.stderr
    assert log.index(' pull') < log.index('image inspect') < log.index('up -d --wait db')
    assert 'org.opencontainers.image.revision' in log and 'dev.line1.ai-revision' in log


def test_worker_uses_same_image_and_storage_without_publishing_a_port():
    import yaml
    compose = yaml.safe_load((ROOT / 'deploy/compose.yml').read_text())
    api, worker = compose['services']['api'], compose['services']['grading-worker']
    assert worker['image'] == api['image'] and worker['volumes'] == api['volumes']
    assert 'ports' not in worker
    assert worker['command'] == ['python', '-m', 'app.workers.grading', '--loop']
    assert worker['restart'] == 'unless-stopped' and worker['stop_grace_period'] == '35s'
    assert worker['labels']['dev.line1.database-writer'] == 'true'
    assert worker['healthcheck']['test'][:3] == ['CMD', 'python', '-c']
    assert worker['healthcheck']['test'][-1] == '/tmp/grading-worker-health.json'


def test_exact_project_writers_including_orphans_stop_before_backup(deployment):
    rows = '\n'.join([
        '111111111111|line1-2026|grading-worker|true',
        '222222222222|line1-2026|future-worker|true',
        '333333333333|line1-2026|db|true',
        '444444444444|unrelated-project|grading-worker|true',
        '555555555555|line1-2026|frontend|',
        '666666666666|line1-2026|grading-worker|',
    ])
    result, log = run(deployment, MOCK_CONTAINER_ROWS=rows)
    assert result.returncode == 0, result.stderr
    stopped = next(line for line in log.splitlines() if line.startswith('stop --time'))
    assert stopped == 'stop --time 320 111111111111 222222222222 666666666666'
    assert 'ps --filter label=com.docker.compose.project=line1-2026 ' in log
    assert log.index('stop api') < log.index(stopped) < log.index('pg_dump') < log.index('run --rm --no-deps migrate')
    assert 'up -d --no-deps --wait --wait-timeout 180 api grading-worker' in log


def test_latest_apply_with_old_compose_and_image_stops_orphan_worker_then_starts_api_only(deployment):
    result, log = run(deployment, WORKER_PROBE_STATUS='42', MOCK_SERVICES='db\nmigrate\napi',
        MOCK_CONTAINER_ROWS='111111111111|line1-2026|grading-worker|true')
    assert result.returncode == 0, result.stderr
    assert log.index('stop --time 320 111111111111') < log.index('pg_dump')
    assert 'up -d --no-deps --wait --wait-timeout 180 api\n' in log
    assert 'up -d --no-deps --wait --wait-timeout 180 api grading-worker' not in log


@pytest.mark.parametrize('probe_status', ['1', '2', '137'])
def test_unexpected_module_probe_error_is_not_treated_as_old_image(deployment, probe_status):
    result, log = run(deployment, WORKER_PROBE_STATUS=probe_status, revisions=(BE_REVISION, AI_REVISION))
    assert result.returncode != 0
    assert 'module probe failed' in result.stderr
    assert log.index('image inspect') < log.index('run --rm --entrypoint python')
    assert 'stop api' not in log and 'pg_dump' not in log


def test_worker_image_requires_matching_compose_service_before_stopping_current_api(deployment):
    result, log = run(deployment, MOCK_SERVICES='db\nmigrate\napi')
    assert result.returncode != 0 and 'Compose contract' in result.stderr
    assert 'stop api' not in log and 'pg_dump' not in log


@pytest.mark.parametrize('failure', ['ps --filter', 'stop --time 320'])
def test_writer_discovery_or_stop_failure_prevents_backup_and_migration(deployment, failure):
    result, log = run(deployment, FAIL_MATCH=failure,
        MOCK_CONTAINER_ROWS='111111111111|line1-2026|grading-worker|true')
    assert result.returncode != 0
    assert 'pg_dump' not in log and 'run --rm --no-deps migrate' not in log


def test_apply_shell_syntax_is_valid():
    result = subprocess.run(['bash', '-n', str(ROOT / 'deploy/apply.sh')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('kind,expected', [('missing_parent', 42), ('missing_module', 42),
    ('missing_dependency', 1), ('broken_module', 1), ('valid', 0)])
@pytest.mark.parametrize('worker_module', ['grading', 'materials'])
def test_actual_probe_distinguishes_old_module_absence_from_import_failure(tmp_path, kind, expected, worker_module):
    app = tmp_path / 'app'
    app.mkdir()
    (app / '__init__.py').write_text('')
    if kind != 'missing_parent':
        workers = app / 'workers'
        workers.mkdir()
        (workers / '__init__.py').write_text('')
        if kind != 'missing_module':
            content = {'missing_dependency': 'import absent_grading_dependency\n',
                       'broken_module': 'raise RuntimeError("broken worker")\n', 'valid': ''}[kind]
            (workers / (worker_module + '.py')).write_text(content)
    services = app / 'services'
    services.mkdir()
    (services / '__init__.py').write_text('')
    (services / 'material_analysis.py').write_text('')
    source = (ROOT / 'deploy/apply.sh').read_text()
    prefix = "material_probe='" if worker_module == "materials" else "probe='"
    probe = source.split(prefix, 1)[1].split("'\nif docker run", 1)[0]
    bootstrap = 'import sys; sys.path.insert(0, sys.argv[1])\n' + probe
    result = subprocess.run([sys.executable, '-I', '-c', bootstrap, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == expected, result.stderr


def test_material_worker_shares_owned_storage_and_has_no_listening_port():
    import yaml
    services = yaml.safe_load((ROOT/'deploy/compose.yml').read_text())['services']
    api, worker = services['api'], services['material-worker']
    assert worker['image'] == api['image'] and worker['volumes'] == api['volumes']
    assert worker['environment'] == api['environment'] and 'ports' not in worker
    assert worker['command'] == ['python','-m','app.workers.materials','--loop']
    assert worker['stop_grace_period'] == '320s'
    assert worker['labels']['dev.line1.database-writer'] == 'true'
    assert worker['healthcheck']['test'][:3] == ['CMD', 'python', '-c']
    assert worker['healthcheck']['test'][-1] == '/tmp/material-worker-health.json'


def test_material_image_without_compose_worker_stops_before_writes(deployment):
    result, log = run(deployment, MOCK_SERVICES='db\nmigrate\napi\ngrading-worker')
    assert result.returncode != 0
    assert 'material worker' in result.stderr and 'Compose contract' in result.stderr
    assert 'stop api' not in log and 'pg_dump' not in log


def test_material_worker_drains_before_backup_and_restarts_after_migration(deployment):
    result, log = run(deployment, MOCK_CONTAINER_ROWS='111111111111|line1-2026|material-worker|true')
    assert result.returncode == 0, result.stderr
    assert log.index('stop --time 320 111111111111') < log.index('pg_dump')
    assert 'up -d --no-deps --wait --wait-timeout 180 api grading-worker material-worker' in log
