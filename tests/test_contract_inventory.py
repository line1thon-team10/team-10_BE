from fastapi import APIRouter, FastAPI

from scripts.check_api_spec import operation_key, runtime_gate_passes, runtime_inventory


def test_required_missing_is_pending_and_blocks_release_but_optional_does_not():
    app = FastAPI()
    app.get("/api/ready/{id}")(lambda id: {})
    rows = [{"method": "GET", "path": "/api/ready/:subjectId"},
            {"method": "POST", "path": "/api/pending"},
            {"method": "POST", "path": "/api/optional", "optional": True}]
    report = runtime_inventory(app, rows)
    assert report["required_registered"] == [("GET", "/api/ready/:param")]
    assert report["required_pending"] == [("POST", "/api/pending")]
    assert report["optional_pending"] == [("POST", "/api/optional")]
    assert not runtime_gate_passes(report, require_complete=True)
    app.post("/api/pending")(lambda: {})
    assert runtime_gate_passes(runtime_inventory(app, rows), require_complete=True)


def test_lazy_nested_router_prefixes_and_hidden_duplicate_paths_are_checked():
    app, parent, child = FastAPI(), APIRouter(), APIRouter()
    child.get("/{item_id}", include_in_schema=False)(lambda item_id: {})
    child.get("/{other_id}")(lambda other_id: {})
    parent.include_router(child, prefix="/items")
    app.include_router(parent, prefix="/api")
    report = runtime_inventory(app, [{"method": "GET", "path": "/api/items/:id"}])
    assert report["required_registered"] == [("GET", "/api/items/:param")]
    assert report["duplicates"] == [("GET", "/api/items/:param")]
    assert not runtime_gate_passes(report)


def test_mount_prefix_is_kept_and_uncontracted_hidden_api_is_not_ignored():
    app, mounted = FastAPI(), FastAPI()
    mounted.post("/secret", include_in_schema=False)(lambda: {})
    app.mount("/api", mounted)
    report = runtime_inventory(app, [])
    assert ("POST", "/api/secret") in report["uncontracted"]
    assert not runtime_gate_passes(report)


def test_actual_routes_cover_every_required_contract_without_duplicates():
    from app.main import app
    report = runtime_inventory(app)
    assert runtime_gate_passes(report), report
    assert report["required_total"] == len(report["required_registered"]) + len(report["required_pending"])
    # Public registration is complete; behavior is covered by the HTTP suites.
    assert runtime_gate_passes(report, require_complete=True), report


def test_path_parameter_names_do_not_hide_literal_route_differences():
    assert operation_key("get", "/api/quiz/{key}") == operation_key("GET", "/api/quiz/:subjectId")
    assert operation_key("GET", "/api/quiz/today") != operation_key("GET", "/api/quiz/:id")
