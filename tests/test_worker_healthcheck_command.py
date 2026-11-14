"""Execute the deployed probe without importing installed app dependencies."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICES = yaml.safe_load((ROOT / 'deploy/compose.yml').read_text())['services']


@pytest.fixture(params=['grading-worker', 'material-worker'])
def probe(request):
    health = SERVICES[request.param]['healthcheck']
    assert health['timeout'] == '5s'
    command = health['test']
    assert command[:3] == ['CMD', 'python', '-c']
    assert command[4] == f'/tmp/{request.param}-health.json'
    imports = [alias.name for node in ast.walk(ast.parse(command[3]))
               if isinstance(node, ast.Import) for alias in node.names]
    assert set(imports) == {'json', 'math', 'os', 'sys', 'time'}
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(ast.parse(command[3])))
    return command[3]


def run_probe(probe, path):
    # -S excludes site packages; the real existing image only needs stdlib.
    return subprocess.run([sys.executable, '-S', '-c', probe, str(path)],
                          capture_output=True, text=True, timeout=5)


def test_fresh_live_pid_is_healthy_with_short_or_long_worker_budget(probe, tmp_path):
    path = tmp_path / 'heartbeat'
    for lifespan in (31, 315):
        path.write_text(json.dumps({'pid': os.getpid(), 'expires_at': time.time() + lifespan}))
        # Validity follows expires_at, not a guessed fixed mtime threshold.
        os.utime(path, (time.time() - 1000, time.time() - 1000))
        assert run_probe(probe, path).returncode == 0


@pytest.mark.parametrize('case', [
    'expired', 'dead_pid', 'malformed', 'bool_pid', 'string_pid', 'zero_pid',
    'negative_pid', 'huge_pid', 'nan_expiry', 'infinite_expiry', 'negative_infinite_expiry',
    'bool_expiry', 'string_expiry', 'missing_expiry', 'array', 'null', 'missing_file',
])
def test_invalid_or_dead_heartbeat_is_unhealthy(probe, tmp_path, case):
    path = tmp_path / 'heartbeat'
    value = {'pid': os.getpid(), 'expires_at': time.time() + 315}
    if case == 'expired':
        value['expires_at'] = time.time() - 1
    elif case == 'dead_pid':
        process = subprocess.run([sys.executable, '-S', '-c', 'import os; print(os.getpid())'],
                                 capture_output=True, text=True, check=True)
        value['pid'] = int(process.stdout)
    elif case.endswith('_pid'):
        value['pid'] = {'bool_pid': True, 'string_pid': str(os.getpid()), 'zero_pid': 0,
                        'negative_pid': -1, 'huge_pid': 10 ** 100}[case]
    elif case.endswith('_expiry') and case != 'missing_expiry':
        value['expires_at'] = {'nan_expiry': float('nan'), 'infinite_expiry': float('inf'),
                              'negative_infinite_expiry': -float('inf'), 'bool_expiry': True,
                              'string_expiry': str(time.time() + 315)}[case]
    elif case == 'missing_expiry':
        del value['expires_at']
    elif case == 'array':
        value = []
    elif case == 'null':
        value = None
    if case != 'missing_file':
        path.write_text('{broken' if case == 'malformed' else json.dumps(value))
    result = run_probe(probe, path)
    assert result.returncode == 1, result.stderr
