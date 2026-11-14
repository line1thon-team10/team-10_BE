"""Opt-in deterministic pytest shards; never loaded through conftest or env vars.

Use: python -m pytest -p scripts.pytest_shard tests --contract-shard-count=4
     --contract-shard-index=0 --contract-shard-manifest=/tmp/shard-0.json
"""
import hashlib
import json
from pathlib import Path

import pytest


def shard_for(nodeid: str, count: int) -> int:
    if type(count) is not int or count < 1:
        raise ValueError('Shard count must be a positive integer')
    return int.from_bytes(hashlib.sha256(nodeid.encode('utf-8')).digest(), 'big') % count


def pytest_addoption(parser):
    group = parser.getgroup('contract shards')
    group.addoption('--contract-shard-index', type=int, default=None)
    group.addoption('--contract-shard-count', type=int, default=None)
    group.addoption('--contract-shard-manifest', default=None)


def pytest_configure(config):
    index = config.getoption('--contract-shard-index')
    count = config.getoption('--contract-shard-count')
    manifest = config.getoption('--contract-shard-manifest')
    if index is None and count is None and manifest is None:
        config._contract_shard = None
        return
    if index is None or count is None or not 0 <= index < count:
        raise pytest.UsageError('Use both --contract-shard-count=N (N>0) and --contract-shard-index=0..N-1')
    config._contract_shard = {'index': index, 'count': count, 'manifest': manifest, 'collected': [], 'selected': []}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    state = config._contract_shard
    if state is None:
        return
    selected, deselected = [], []
    for item in items:
        (selected if shard_for(item.nodeid, state['count']) == state['index'] else deselected).append(item)
    state['collected'] = sorted(item.nodeid for item in items)
    state['selected'] = sorted(item.nodeid for item in selected)
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    if state['manifest']:
        manifest = Path(state['manifest'])
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            'schemaVersion': 1, 'index': state['index'], 'count': state['count'],
            'collected': state['collected'], 'selected': state['selected'],
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def pytest_sessionfinish(session, exitstatus):
    state = session.config._contract_shard
    # An empty partition of a nonempty collection is valid. Do not hide an
    # actually empty collection, collection error, interrupted run or failure.
    if (state is not None and state['collected'] and not state['selected']
            and exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.OK


def pytest_terminal_summary(terminalreporter):
    state = terminalreporter.config._contract_shard
    if state is None:
        return
    digest = hashlib.sha256('\n'.join(state['collected']).encode('utf-8')).hexdigest()
    terminalreporter.write_line(f"Contract shard {state['index'] + 1}/{state['count']}: "
        f"{len(state['selected'])}/{len(state['collected'])} collected items; collection SHA256 {digest}")
