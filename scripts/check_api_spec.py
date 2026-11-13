"""Check documentation links and method/path coverage against a local FE checkout.

Usage: python3 scripts/check_api_spec.py --fe ../FE
This checks documentation inventory, not runtime API conformance.
"""
import argparse
import json
from pathlib import Path
import re

parser = argparse.ArgumentParser()
parser.add_argument('--fe', type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
docs = root / 'docs/api'
rows = json.loads((docs / 'operations.json').read_text())
actual = {(r['method'], r['path']) for r in rows}
assert len(actual) == len(rows), 'Duplicate operation'
for row in rows:
    assert row['method'] in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}
    assert row['path'].startswith('/api/')
    assert (docs / row['contract']).is_file(), row
    assert f"| {row['method']} | `{row['path']}` |" in (docs / 'operations.md').read_text(), row
for file in docs.glob('*.md'):
    for target in re.findall(r'\]\(([^)]+)\)', file.read_text()):
        if '://' in target or target.startswith('#'):
            continue
        assert (file.parent / target.split('#')[0]).is_file(), (file.name, target)
endpoints = dict(re.findall(r"(\w+):\s*'([^']+)'", (args.fe / 'src/api/endpoints.js').read_text()))
expected = set()
for file in (args.fe / 'src/mocks/handlers').glob('*.js'):
    text = file.read_text()
    matches = re.findall(r'http\.(get|post|patch|put|delete)\(\s*(EP\.\w+|`[^`]+`)', text)
    assert len(matches) == len(re.findall(r'http\.(?:get|post|patch|put|delete)\(', text)), f'Unparsed handler: {file}'
    for method, expression in matches:
        path = endpoints[expression[3:]] if expression.startswith('EP.') else re.sub(
            r'\$\{EP\.(\w+)\}', lambda m: endpoints[m[1]], expression[1:-1])
        expected.add((method.upper(), path))
frontend = {(r['method'],r['path']) for r in rows if not r.get('origin')}
proposed = actual - frontend
assert expected == frontend, {'missing': sorted(expected-frontend), 'extra': sorted(frontend-expected)}
allowed_origins = {'wireframe-S8', 'BE-schedule-edit', 'BE-subject-draft'}
for row in rows:
    if row.get('origin'):
        assert row['origin'] in allowed_origins, row
        assert isinstance(row.get('source'), str) and row['source'].strip(), row
print(f'PASS: {len(frontend)} FE operations + {len(proposed)} source-derived operations, unique registry, contract files and Markdown links')
print('Runtime behavior and field-schema conformance are NOT verified by this check.')
