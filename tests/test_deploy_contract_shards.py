"""Deployment validates the exact app revision before building or publishing."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/deploy-rpi.yml').read_text())
JOBS = WORKFLOW['jobs']


def named_steps(job):
    return {step.get('name'): step for step in job['steps']}


def test_deployment_has_four_isolated_contract_shards_before_image_build():
    tests, build, deploy = (JOBS[name] for name in ('contract_tests', 'build', 'deploy'))
    assert tests['strategy'] == {'fail-fast': False, 'matrix': {'shard': [0, 1, 2, 3]}}
    assert tests['timeout-minutes'] == 45
    assert tests['services']['postgres']['image'] == 'postgres:17-alpine'
    assert 'packages' not in tests.get('permissions', WORKFLOW['permissions'])
    step = named_steps(tests)['Validate SQLite and PostgreSQL behavior']
    assert step['env']['TEST_POSTGRES_URL'].endswith(':5432/team10_test')
    assert '--contract-shard-count=4 --contract-shard-index=${{ matrix.shard }}' in step['run']
    assert '-p pytest_shard' in step['run']
    assert 'PYTEST_ADDOPTS' not in step['run'] and 'PYTEST_PLUGINS' not in step['run']
    assert build['needs'] == 'contract_tests'
    assert 'services' not in build
    assert not any('pytest' in step.get('run', '') for step in build['steps'])
    assert 'Validate deployment compose' in named_steps(build)
    assert deploy['needs'] == 'build'
    assert build['outputs']['revision'] == '${{ steps.ai.outputs.revision }}'
    assert build['outputs']['ai_revision'] == '${{ steps.ai.outputs.sha }}'


@pytest.mark.parametrize('matrix_result,build_allowed', [
    ('success', True), ('failure', False), ('cancelled', False), ('skipped', False), ('', False),
])
def test_build_gate_rejects_any_unsuccessful_matrix_result(matrix_result, build_allowed):
    # Check the exact GitHub dependency expression, including its job name.
    # Any failed/cancelled/skipped matrix result must fail this comparison.
    expression = JOBS['build']['if']
    match = re.fullmatch(r"\$\{\{ needs\.contract_tests\.result == '([^']+)' \}\}", expression)
    assert match is not None
    assert (matrix_result == match.group(1)) is build_allowed


@pytest.mark.parametrize('job_name', ['contract_tests', 'build'])
def test_tests_and_build_checkout_the_same_immutable_application_and_private_ai(job_name):
    job = JOBS[job_name]
    steps = named_steps(job)
    manual = steps['Validate manual revision']
    assert manual['env']['REVISION'] == '${{ inputs.revision }}'
    assert '^[a-f0-9]{40}$' in manual['run']
    checkout = next(step for step in job['steps'] if step.get('uses', '').startswith('actions/checkout@'))
    assert checkout['with']['ref'] == '${{ inputs.revision || github.sha }}'
    assert checkout['with']['persist-credentials'] is False
    exact = steps['Verify exact application revision']
    assert exact['env']['EXPECTED_REVISION'] == checkout['with']['ref']
    assert 'git rev-parse HEAD' in exact['run']
    assert 'HEAD:vendor/team-10_AI' in steps['Read pinned AI commit']['run']
    ai = next(step for step in job['steps'] if step.get('with', {}).get('path') == 'vendor/team-10_AI')
    assert ai['with']['ref'] == '${{ steps.ai.outputs.sha }}'
    assert ai['with']['ssh-key'] == '${{ secrets.AI_SUBMODULE_SSH_KEY }}'
    assert steps['Verify exact AI revision']['env']['EXPECTED_AI'] == ai['with']['ref']


def test_old_application_uses_verified_workflow_plugin_without_auto_loading_it(tmp_path):
    job = JOBS['contract_tests']
    checkout = next(step for step in job['steps'] if step.get('with', {}).get('path') == '.contract-tools')
    assert checkout['with']['ref'] == '${{ github.workflow_sha }}'
    steps = named_steps(job)
    assert steps['Verify contract tool revision']['env']['EXPECTED_TOOLS'] == '${{ github.workflow_sha }}'
    assert 'git -C .contract-tools rev-parse HEAD' in steps['Verify contract tool revision']['run']
    command = steps['Validate SQLite and PostgreSQL behavior']['run']
    assert 'PYTHONPATH="$GITHUB_WORKSPACE/.contract-tools/scripts${PYTHONPATH:+:$PYTHONPATH}"' in command

    # This application checkout intentionally has no scripts/pytest_shard.py.
    # Execute the same standalone plugin import used by deployment rollback.
    old_app = tmp_path / 'old-app'
    old_app.mkdir()
    tool_scripts = old_app / '.contract-tools' / 'scripts'
    tool_scripts.mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/pytest_shard.py', tool_scripts)
    (old_app / 'test_app.py').write_text('import pytest\n@pytest.mark.parametrize("value", range(20))\ndef test_old(value): pass\n')
    env = {**os.environ, 'PYTHONPATH': str(tool_scripts), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    env.pop('PYTEST_ADDOPTS', None)
    env.pop('PYTEST_PLUGINS', None)
    selected = Counter()
    for index in range(4):
        manifest = old_app / f'shard-{index}.json'
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'test_app.py', '-p', 'pytest_shard',
                                 '--contract-shard-count=4', f'--contract-shard-index={index}',
                                 f'--contract-shard-manifest={manifest}'], cwd=old_app, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(manifest.read_text())
        selected.update(data['selected'])
    assert selected == Counter(data['collected']) and len(selected) == 20
    plain = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'test_app.py'], cwd=old_app,
                           env=env, capture_output=True, text=True, timeout=30)
    assert plain.returncode == 0 and '20 passed' in plain.stdout
