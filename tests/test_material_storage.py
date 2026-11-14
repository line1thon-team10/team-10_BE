import hashlib
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import select, update

from app.core.clock import now_utc
from app.db import schema as t
from app.services import material_storage as storage


def pdf_bytes(text="자료 원문"):
    with pymupdf.open() as document:
        document.new_page().insert_text((20, 30), text)
        return document.tobytes()


@pytest.fixture
def material_case(api_client, domain_connection, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "STORAGE_DIR", tmp_path / "uploads")
    conn, now = domain_connection, now_utc()
    ids = {key: str(uuid4()) for key in ("user", "subject", "empty_subject", "other_user", "other_subject")}
    for user in (ids["user"], ids["other_user"]):
        conn.execute(t.users.insert().values(id=user, created_at=now, updated_at=now))
    for subject, user in (("subject", "user"), ("empty_subject", "user"), ("other_subject", "other_user")):
        conn.execute(t.subjects.insert().values(id=ids[subject], user_id=ids[user], code=subject,
            name=subject, color="#123456", created_at=now, updated_at=now))
    conn.commit()
    return {**ids, "headers": {"X-Anonymous-Id": ids["user"]}, "root": tmp_path / "uploads"}


def add_file(conn, case, *, subject="subject", status="pending", method=None, quiz=True, create=True):
    now, material_id, raw = now_utc(), str(uuid4()), pdf_bytes()
    user = case["other_user"] if subject == "other_subject" else case["user"]
    digest = hashlib.sha256(raw).hexdigest()
    key = storage.MaterialStorageService.storage_key(user, material_id, digest)
    conn.execute(t.materials.insert().values(id=material_id, subject_id=case[subject], original_name="강의.pdf",
        source_format="pdf", sha256=digest, original_storage_key=key, storage_key=key, page_count=1,
        analysis_version=material_id, analysis_status=status, extraction_method=method,
        quiz_enabled=quiz, created_at=now, updated_at=now))
    path = case["root"] / key
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    conn.commit()
    return material_id, path, raw


def test_material_list_preserves_multiple_files_empty_subjects_and_unknown_quality(
        api_client, material_case, domain_connection):
    case, conn = material_case, domain_connection
    pending, _, _ = add_file(conn, case)
    native, _, _ = add_file(conn, case, status="succeeded", method="text")
    ocr, _, _ = add_file(conn, case, status="succeeded", method="ocr", quiz=False)
    failed, _, _ = add_file(conn, case, status="failed", method="ocr")
    foreign, _, _ = add_file(conn, case, subject="other_subject", status="succeeded")
    response = api_client.get("/api/materials", headers=case["headers"])
    assert response.status_code == 200, response.text
    subjects = {row["id"]: row for row in response.json()["data"]["subjects"]}
    assert set(subjects) == {case["subject"], case["empty_subject"]}
    assert subjects[case["empty_subject"]]["files"] == []
    files = {row["id"]: row for row in subjects[case["subject"]]["files"]}
    assert set(files) == {pending, native, ocr, failed} and foreign not in files
    assert files[pending] == {"id": pending, "name": "강의.pdf", "pages": 1, "status": "running",
                              "jobId": None, "quizless": None, "ocr": None}
    assert files[native]["status"] == "done" and files[native]["quizless"] is False and files[native]["ocr"] is False
    assert files[ocr]["status"] == "done" and files[ocr]["quizless"] is True and files[ocr]["ocr"] is True
    assert files[failed]["status"] == "failed" and files[failed]["ocr"] is None
    assert "storage" not in response.text and str(case["root"]) not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_owned_original_pdf_is_binary_and_never_crosses_owner_or_missing_file(
        api_client, material_case, domain_connection):
    case, conn = material_case, domain_connection
    material, path, raw = add_file(conn, case)
    response = api_client.get(f"/api/materials/{material}/file", headers=case["headers"])
    assert response.status_code == 200 and response.content == raw
    assert response.headers["content-type"] == "application/pdf" and response.headers["cache-control"] == "no-store"
    assert str(case["root"]) not in str(response.headers)
    foreign = api_client.get(f"/api/materials/{material}/file", headers={"X-Anonymous-Id": case["other_user"]})
    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "NO_FILE"
    path.unlink()
    missing = api_client.get(f"/api/materials/{material}/file", headers=case["headers"])
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NO_FILE"


@pytest.mark.parametrize("tamper", ["foreign_key", "traversal", "symlink", "deleted"])
def test_download_rejects_unsafe_or_deleted_storage_keys(api_client, material_case, domain_connection, tamper):
    case, conn = material_case, domain_connection
    material, path, _ = add_file(conn, case)
    _, foreign_path, _ = add_file(conn, case, subject="other_subject")
    if tamper == "symlink":
        path.unlink()
        path.symlink_to(foreign_path)
    else:
        values = ({"original_deleted_at": now_utc()} if tamper == "deleted" else {
            "original_storage_key": str(foreign_path.relative_to(case["root"])) if tamper == "foreign_key" else "../../secret.pdf"})
        conn.execute(update(t.materials).where(t.materials.c.id == material).values(**values))
        conn.commit()
    response = api_client.get(f"/api/materials/{material}/file", headers=case["headers"])
    assert response.status_code == 404 and response.json()["error"]["code"] == "NO_FILE"
    assert str(case["root"]) not in response.text
