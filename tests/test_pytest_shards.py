"""Run the opt-in plugin against real miniature pytest sessions."""
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.check_pytest_shards import verify
from scripts.pytest_shard import shard_for

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ['-p', 'scripts.pytest_shard']


def run(directory, *args):
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    env.pop('PYTEST_ADDOPTS', None)
    env.pop('PYTEST_PLUGINS', None)
    return subprocess.run([sys.executable, '-m', 'pytest', '-q', *args],
                          cwd=directory, env=env, capture_output=True, text=True, timeout=45)


@pytest.fixture
def suite(tmp_path):
    (tmp_path / 'test_sample.py').write_text('''
import json
from pathlib import Path
import pytest
@pytest.mark.parametrize('value', range(24), ids=lambda value: f'edge-{value}')
def test_sample(value, request):
    with Path('executed.jsonl').open('a') as output:
        output.write(json.dumps(request.node.nodeid) + '\\n')
''')
    return tmp_path


def test_four_shards_execute_every_node_once_and_default_is_unchanged(suite):
    log = suite / 'executed.jsonl'
    baseline = run(suite)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    expected = Counter(json.loads(line) for line in log.read_text().splitlines())
    assert len(expected) == 24
    log.unlink()
    inactive = run(suite, *PLUGIN)
    assert inactive.returncode == 0, inactive.stdout + inactive.stderr
    assert Counter(json.loads(line) for line in log.read_text().splitlines()) == expected
    log.unlink()
    manifests = []
    for index in range(4):
        result = run(suite, *PLUGIN, '--contract-shard-count=4', f'--contract-shard-index={index}',
                     f'--contract-shard-manifest={index}.json')
        assert result.returncode == 0, result.stdout + result.stderr
        manifest = json.loads((suite / f'{index}.json').read_text())
        assert all(shard_for(node, 4) == index for node in manifest['selected'])
        manifests.append(manifest)
    assert Counter(json.loads(line) for line in log.read_text().splitlines()) == expected
    assert verify(manifests) == (24, [len(item['selected']) for item in manifests])
    checked = subprocess.run([sys.executable, str(ROOT / 'scripts/check_pytest_shards.py'),
                              *(str(suite / f'{index}.json') for index in range(4))],
                             capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize('options', [
    ['--contract-shard-count=4'], ['--contract-shard-index=0'],
    ['--contract-shard-count=0', '--contract-shard-index=0'],
    ['--contract-shard-count=4', '--contract-shard-index=-1'],
    ['--contract-shard-count=4', '--contract-shard-index=4'],
    ['--contract-shard-manifest=unused.json'],
])
def test_invalid_configuration_stops_before_execution(suite, options):
    result = run(suite, *PLUGIN, *options)
    assert result.returncode == 4
    assert not (suite / 'executed.jsonl').exists()


@pytest.mark.parametrize('source,expected', [
    ('def test_failure(): assert False', 1),
    ('raise RuntimeError("collection failure")', 2),
    ('', 5),
])
def test_failure_and_real_empty_collection_are_not_hidden(tmp_path, source, expected):
    (tmp_path / 'test_case.py').write_text(source)
    result = run(tmp_path, *PLUGIN, '--contract-shard-count=1', '--contract-shard-index=0')
    assert result.returncode == expected, result.stdout + result.stderr


def test_empty_partition_is_success(tmp_path):
    (tmp_path / 'test_case.py').write_text('def test_ok(): pass')
    index = 1 - shard_for('test_case.py::test_ok', 2)
    result = run(tmp_path, *PLUGIN, '--contract-shard-count=2', f'--contract-shard-index={index}')
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_pytest_does_not_inherit_outer_shard(tmp_path):
    inner = tmp_path / 'nested'
    inner.mkdir()
    (inner / 'test_inner.py').write_text('import pytest\n@pytest.mark.parametrize("v", range(9))\ndef test_inner(v): pass')
    (tmp_path / 'test_outer.py').write_text('''
import subprocess, sys
def test_outer():
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'nested'], capture_output=True, text=True)
    assert result.returncode == 0
    assert '9 passed' in result.stdout
''')
    index = shard_for('test_outer.py::test_outer', 4)
    result = run(tmp_path, 'test_outer.py', *PLUGIN, '--contract-shard-count=4', f'--contract-shard-index={index}')
    assert result.returncode == 0, result.stdout + result.stderr


def test_assignment_is_stable_across_python_hash_seeds_and_unicode(tmp_path):
    code = 'from scripts.pytest_shard import shard_for; print(shard_for("파일.py::test_x[é]", 4))'
    values = []
    for seed in ['1', 'random', '345']:
        result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path,
                                env={**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONHASHSEED': seed},
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        values.append(result.stdout)
    assert len(set(values)) == 1


@pytest.mark.parametrize('damage', ['missing_manifest', 'duplicate_index', 'collection_drift',
                                   'missing_test', 'duplicate_test', 'uncollected_test', 'empty_collection'])
def test_manifest_verifier_rejects_incomplete_or_inconsistent_partitions(damage):
    manifests = [dict(schemaVersion=1, count=2, index=index, collected=['a', 'b'], selected=[node])
                 for index, node in enumerate(['a', 'b'])]
    manifests = deepcopy(manifests)
    if damage == 'missing_manifest':
        manifests.pop()
    elif damage == 'duplicate_index':
        manifests[1]['index'] = 0
    elif damage == 'collection_drift':
        manifests[1]['collected'].append('c')
    elif damage == 'missing_test':
        manifests[1]['selected'] = []
    elif damage == 'duplicate_test':
        manifests[1]['selected'].append('a')
    elif damage == 'uncollected_test':
        manifests[1]['selected'].append('c')
    else:
        for manifest in manifests:
            manifest['collected'] = []
    with pytest.raises(ValueError):
        verify(manifests)
