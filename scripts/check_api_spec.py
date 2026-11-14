"""Documentation coverage and separately opt-in runtime route inventory.

The route inventory does not establish response-field or business conformance.
Use --require-complete as a release gate; pending routes are never counted ready.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_API = {("GET", "/api/health"), ("POST", "/api/session")}


def operation_key(method, path):
    """Match named path parameters across FE and FastAPI naming conventions."""
    path = re.sub(r"\{[^{}]+\}|:[^/]+", ":param", path)
    return method.upper(), path


def http_routes(routes, prefix=""):
    """Walk effective included routes, preserving nested-router/mount prefixes.

    FastAPI 0.162 keeps included routers lazy; app.routes is not a flat list.
    Read the routing candidates rather than OpenAPI, which drops hidden routes
    and overwrites duplicates.
    """
    for route in routes:
        if callable(getattr(route, "effective_candidates", None)):
            yield from http_routes(route.effective_candidates(), prefix)
        elif hasattr(route, "routes"):
            yield from http_routes(route.routes, prefix + route.path)
        else:
            path = prefix + getattr(route, "path", "")
            for method in getattr(route, "methods", ()) or ():
                yield method, path


def runtime_inventory(app, rows=None):
    if rows is None:
        rows = json.loads((ROOT / "docs/api/operations.json").read_text())
    expected = {operation_key(row["method"], row["path"]): row for row in rows}
    if len(expected) != len(rows):
        raise ValueError("Ambiguous contract paths after parameter normalization")
    routes = [operation_key(method, path) for method, path in http_routes(app.routes)
              if path.startswith("/api/")]
    counts = Counter(routes)
    registered = set(counts)
    extras = registered - expected.keys() - {("GET", "/api/health")}
    required = {key for key, row in expected.items() if not row.get("optional", False)}
    optional = set(expected) - required
    return {
        "required_total": len(required),
        "required_registered": sorted(required & registered),
        "required_pending": sorted(required - registered),
        "optional_registered": sorted(optional & registered),
        "optional_pending": sorted(optional - registered),
        "uncontracted": sorted(extras),
        "duplicates": sorted(key for key, count in counts.items() if count > 1),
    }


def runtime_gate_passes(inventory, require_complete=False):
    return not (inventory["uncontracted"] or inventory["duplicates"] or
                (require_complete and inventory["required_pending"]))


def check_documentation(fe):
    root = ROOT
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
    endpoints = dict(re.findall(r"(\w+):\s*'([^']+)'", (fe / 'src/api/endpoints.js').read_text()))
    expected = set()
    for file in (fe / 'src/mocks/handlers').glob('*.js'):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fe", type=Path, help="FE checkout for the documentation check")
    parser.add_argument("--runtime", action="store_true", help="Report actual /api route registration")
    parser.add_argument("--require-complete", action="store_true", help="Fail while required routes are pending")
    args = parser.parse_args(argv)
    if not args.fe and not (args.runtime or args.require_complete):
        parser.error("Pass --fe or --runtime (or --require-complete)")
    if args.fe:
        check_documentation(args.fe)
    if args.runtime or args.require_complete:
        # Running this file directly puts scripts/, rather than the repo, on sys.path.
        import sys
        sys.path.insert(0, str(ROOT))
        from app.main import app
        inventory = runtime_inventory(app)
        print(json.dumps(inventory, ensure_ascii=False, indent=2))
        print("Route registration only: response fields, ownership and business behavior require HTTP tests.")
        if not runtime_gate_passes(inventory, args.require_complete):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
