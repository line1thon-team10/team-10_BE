"""The required check remains a strict gate over isolated contract jobs."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

WORKFLOW = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/pr-checks.yml').read_text())


def test_four_independent_database_jobs_keep_private_ai_revision_guards():
    job = WORKFLOW['jobs']['shards']
    assert job['strategy'] == {'fail-fast': False, 'matrix': {'shard': [0, 1, 2, 3]}}
    assert job['services']['postgres']['image'] == 'postgres:17-alpine'
    steps = {step.get('name'): step for step in job['steps']}
    trusted = steps['Require trusted private AI access']['run']
    assert 'HEAD_REPOSITORY' in trusted and 'BASE_REPOSITORY' in trusted and 'AI_DEPLOY_KEY' in trusted
    assert 'exit 1' in trusted
    assert 'HEAD:vendor/team-10_AI' in steps['Read pinned AI revision']['run']
    ai_checkout = next(step for step in job['steps'] if step.get('with', {}).get('path') == 'vendor/team-10_AI')
    assert ai_checkout['with']['ref'] == '${{ steps.revisions.outputs.ai }}'
    assert ai_checkout['with']['ssh-key'] == '${{ secrets.AI_SUBMODULE_SSH_KEY }}'
    assert 'EXPECTED_AI' in steps['Verify exact AI revision']['run']
    run = steps['Validate SQLite and PostgreSQL behavior']
    assert run['env']['TEST_POSTGRES_URL'].endswith(':5432/team10_test')
    assert '-p scripts.pytest_shard' in run['run']
    assert '--contract-shard-count=4 --contract-shard-index=${{ matrix.shard }}' in run['run']
    assert 'PYTEST_ADDOPTS' not in run['env'] and 'PYTEST_PLUGINS' not in run['env']


@pytest.mark.parametrize('status,expected', [('success', 0), ('failure', 1), ('cancelled', 1), ('skipped', 1), ('', 1)])
def test_required_test_gate_accepts_only_all_success(status, expected):
    gate = WORKFLOW['jobs']['test']
    assert gate['name'] == 'test'
    assert gate['if'] == '${{ always() }}'
    assert gate['needs'] == ['shards']
    step = gate['steps'][0]
    assert step['env']['SHARD_RESULT'] == '${{ needs.shards.result }}'
    result = subprocess.run(['bash', '-c', step['run']], env={**os.environ, 'SHARD_RESULT': status},
                            capture_output=True, text=True)
    assert result.returncode == expected
