import hashlib
import asyncio
from io import BytesIO
from uuid import UUID, uuid4

import pymupdf
import pytest
from sqlalchemy import event, select, update
from starlette.datastructures import Headers

from app.core.clock import now_utc
from app.db import schema as t
from app.repositories.locking import lock_user
from app.repositories.materials import MaterialRepository
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


def upload(client, case, raw, *, subject=None, mime="application/pdf", filename="강의.pdf", headers=None):
    return client.post("/api/materials", headers={**case["headers"], **(headers or {})},
        data={"subjectId": subject or case["subject"]}, files={"file": (filename, raw, mime)})


def assert_no_admission(conn, case):
    assert conn.execute(select(t.materials)).first() is None
    assert conn.execute(select(t.ai_jobs)).first() is None
    assert not [path for path in case["root"].rglob("*") if path.is_file()]
    conn.rollback()


def test_upload_stores_original_and_durable_job_before_200_without_analysis(
        api_client, material_case, domain_connection):
    case, conn, raw = material_case, domain_connection, pdf_bytes("first")
    response = upload(api_client, case, raw, filename="../../강의.pdf")
    assert response.status_code == 200, response.text
    accepted = response.json()["data"]
    assert set(accepted) == {"materialId", "jobId"}
    material = conn.execute(select(t.materials)).mappings().one()
    job = conn.execute(select(t.ai_jobs)).mappings().one()
    assert material["id"] == accepted["materialId"] and material["original_name"] == "강의.pdf"
    assert material["sha256"] == hashlib.sha256(raw).hexdigest() and material["analysis_status"] == "pending"
    assert job["id"] == accepted["jobId"] and job["user_id"] == case["user"] and job["status"] == "queued"
    assert job["kind"] == "pdf_analysis" and job["target_id"] == material["id"] and job["target_type"] == "material"
    assert job["input_payload"] == {"schemaVersion": "material-analysis-input-v1", "materialId": material["id"],
        "subjectId": case["subject"], "sourceSha256": material["sha256"], "storageKey": material["storage_key"],
        "analysisVersion": material["analysis_version"],
        "structureHash": MaterialRepository(conn).structure_hash(case["user"], material["id"])}
    assert job["result"] is None and job["started_at"] is None and job["finished_at"] is None
    assert conn.execute(select(t.units)).first() is None and conn.execute(select(t.ai_usage_logs)).first() is None
    assert (case["root"] / material["original_storage_key"]).read_bytes() == raw
    assert not list((case["root"] / ".uploads").iterdir())
    conn.commit()
    assert api_client.get(f"/api/materials/{material['id']}/file", headers=case["headers"]).content == raw
    second = upload(api_client, case, pdf_bytes("second"))
    assert second.status_code == 200 and second.json()["data"]["materialId"] != material["id"]
    files = next(subject["files"] for subject in api_client.get("/api/materials", headers=case["headers"]).json()["data"]["subjects"]
                 if subject["id"] == case["subject"])
    assert len(files) == 2 and all(file["status"] == "running" and file["ocr"] is None and file["quizless"] is None for file in files)
    body_schema = api_client.get("/openapi.json").json()["paths"]["/api/materials"]["post"]["requestBody"]
    assert body_schema["content"]["multipart/form-data"]["schema"]["required"] == ["subjectId", "file"]


@pytest.mark.parametrize("state", ["queued", "running", "succeeded"])
def test_same_sha_reuses_owned_subject_job_and_preserves_other_pdfs(
        api_client, material_case, domain_connection, state):
    case, conn, raw = material_case, domain_connection, pdf_bytes()
    first = upload(api_client, case, raw)
    assert first.status_code == 200, first.text
    ids = first.json()["data"]
    conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == ids["jobId"]).values(status=state))
    conn.commit()
    again = upload(api_client, case, raw, filename="다른 이름.pdf")
    assert again.status_code == 200 and again.json() == first.json()
    assert len(conn.execute(select(t.materials)).all()) == len(conn.execute(select(t.ai_jobs)).all()) == 1
    conn.commit()
    other_subject = upload(api_client, case, raw, subject=case["empty_subject"])
    assert other_subject.status_code == 200 and other_subject.json()["data"]["materialId"] != ids["materialId"]
    assert len(conn.execute(select(t.materials)).all()) == 2


def test_failed_analysis_gets_new_job_and_preserves_old_attempt(api_client, material_case, domain_connection):
    case, conn, raw = material_case, domain_connection, pdf_bytes()
    first = upload(api_client, case, raw).json()["data"]
    conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == first["jobId"]).values(status="failed", error_code="AI_FAILED"))
    conn.execute(update(t.materials).where(t.materials.c.id == first["materialId"]).values(analysis_status="failed"))
    conn.commit()
    retried = upload(api_client, case, raw)
    assert retried.status_code == 200, retried.text
    second = retried.json()["data"]
    assert second["materialId"] == first["materialId"] and second["jobId"] != first["jobId"]
    jobs = {row["id"]: row for row in conn.execute(select(t.ai_jobs)).mappings()}
    assert jobs[first["jobId"]]["status"] == "failed" and jobs[first["jobId"]]["error_code"] == "AI_FAILED"
    assert jobs[second["jobId"]]["status"] == "queued"
    assert len(conn.execute(select(t.materials)).all()) == 1


def test_legacy_without_file_or_job_enqueues_without_rewriting_structure(api_client, material_case, domain_connection):
    case, conn = material_case, domain_connection
    material, _, raw = add_file(conn, case, status="succeeded", create=False)
    conn.execute(update(t.materials).where(t.materials.c.id == material).values(original_storage_key=None, storage_key=None))
    unit, concept, now = str(uuid4()), str(uuid4()), now_utc()
    conn.execute(t.units.insert().values(id=unit, material_id=material, code="one", title="보존", sort_order=1,
        start_page=1, end_page=1, created_at=now, updated_at=now))
    conn.execute(t.concepts.insert().values(id=concept, unit_id=unit, code="one", title="보존 개념", sort_order=1,
        start_page=1, end_page=1, created_at=now, updated_at=now))
    before = (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all())
    previous_hash = MaterialRepository(conn).structure_hash(case["user"], material)
    conn.commit()
    response = upload(api_client, case, raw)
    assert response.status_code == 200 and response.json()["data"]["materialId"] == material, response.text
    job = conn.execute(select(t.ai_jobs)).mappings().one()
    assert job["status"] == "queued" and job["result"] is None
    assert job["input_payload"]["structureHash"] == previous_hash
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all()) == before
    conn.execute(update(t.concepts).where(t.concepts.c.id == concept).values(in_scope=False))
    assert MaterialRepository(conn).structure_hash(case["user"], material) != previous_hash


def exact_size_pdf(size):
    with pymupdf.open() as document:
        page = document.new_page()
        xref = document.get_new_xref()
        document.update_object(xref, "<<>>")
        document.update_stream(xref, b" ", compress=False)
        page.set_contents(xref)
        amount = size - len(document.tobytes()) + 1
        for _ in range(4):
            document.update_stream(xref, b" " * amount, compress=False)
            raw = document.tobytes()
            if len(raw) == size:
                return raw
            amount += size - len(raw)
    raise AssertionError("Unable to construct the exact PDF boundary")


@pytest.mark.parametrize("over", [0, 1])
def test_actual_25_mib_boundary_ignores_false_content_length(api_client, material_case, domain_connection, over):
    case, conn = material_case, domain_connection
    raw = exact_size_pdf(26214400) + b"\n" * over
    response = upload(api_client, case, raw, headers={"Content-Length": "1"})
    if over:
        assert response.status_code == 413 and response.json()["error"]["code"] == "FILE_TOO_LARGE"
        assert_no_admission(conn, case)
    else:
        assert response.status_code == 200, response.text
        material = conn.execute(select(t.materials)).mappings().one()
        assert (case["root"] / material["original_storage_key"]).stat().st_size == 26214400


@pytest.mark.parametrize("kind,status,code", [("mime", 415, "NOT_PDF"), ("not_pdf", 415, "NOT_PDF"),
    ("corrupt", 400, "BAD_REQUEST"), ("repaired", 400, "BAD_REQUEST"), ("locked", 400, "BAD_REQUEST")])
def test_unreadable_upload_leaves_no_file_or_job(api_client, material_case, domain_connection, kind, status, code):
    raw, mime = pdf_bytes(), "application/pdf"
    if kind == "mime":
        mime = "image/png"
    elif kind == "not_pdf":
        raw = b"not a PDF"
    elif kind == "corrupt":
        raw = b"%PDF-1.7\nbroken"
    elif kind == "repaired":
        raw = raw[:-25]
    else:
        with pymupdf.open(stream=raw, filetype="pdf") as document:
            raw = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
    response = upload(api_client, material_case, raw, mime=mime)
    assert response.status_code == status and response.json()["error"]["code"] == code, response.text
    assert_no_admission(domain_connection, material_case)


@pytest.mark.parametrize("subject", ["foreign", "missing", "invalid"])
def test_upload_refuses_unowned_or_invalid_subject_before_admission(api_client, material_case, domain_connection, subject):
    case = material_case
    value = case["other_subject"] if subject == "foreign" else str(uuid4()) if subject == "missing" else "not-uuid"
    response = upload(api_client, case, pdf_bytes(), subject=value)
    assert response.status_code == (422 if subject == "invalid" else 404), response.text
    assert_no_admission(domain_connection, case)


@pytest.mark.parametrize("failure", ["publish", "enqueue", "commit"])
def test_failed_admission_cleans_file_and_job_then_same_sha_retry_succeeds(
        api_client, material_case, domain_connection, monkeypatch, failure):
    from app.db.session import engine
    case, conn, raw = material_case, domain_connection, pdf_bytes()
    with monkeypatch.context() as patch:
        if failure == "commit":
            def fail_commit(_):
                raise RuntimeError("commit failed before DBAPI commit")
            event.listen(engine, "commit", fail_commit)
        else:
            owner, method = (storage.MaterialStorageService, "_publish") if failure == "publish" else (MaterialRepository, "enqueue")
            original = getattr(owner, method)
            def fail_after_write(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("injected failure after write")
            patch.setattr(owner, method, fail_after_write)
        try:
            response = upload(api_client, case, raw)
            assert response.status_code == 500, response.text
        finally:
            if failure == "commit":
                event.remove(engine, "commit", fail_commit)
    assert_no_admission(conn, case)
    retried = upload(api_client, case, raw)
    assert retried.status_code == 200, retried.text
    assert len(conn.execute(select(t.materials)).all()) == len(conn.execute(select(t.ai_jobs)).all()) == 1


def test_cancelled_admission_rolls_back_file_and_job(material_case, domain_connection, monkeypatch):
    from app.db.session import engine
    original = MaterialRepository.enqueue
    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("cancelled")
    monkeypatch.setattr(MaterialRepository, "enqueue", interrupt)
    case = material_case
    with pytest.raises(KeyboardInterrupt):
        storage.MaterialStorageService(engine).upload(case["user"], case["subject"], BytesIO(pdf_bytes()),
            filename="자료.pdf", content_type="application/pdf")
    assert_no_admission(domain_connection, case)


def test_lost_commit_acknowledgement_preserves_committed_input_and_retry_job(
        api_client, material_case, domain_connection, monkeypatch):
    from app.db.session import engine
    case, conn, raw = material_case, domain_connection, pdf_bytes()
    original = engine.dialect.do_commit
    def committed_but_unknown(connection):
        original(connection)
        raise RuntimeError("connection lost after actual commit")
    with monkeypatch.context() as patch:
        patch.setattr(engine.dialect, "do_commit", committed_but_unknown)
        response = upload(api_client, case, raw)
        assert response.status_code == 500
    material = conn.execute(select(t.materials)).mappings().one()
    job = conn.execute(select(t.ai_jobs)).mappings().one()
    assert (case["root"] / material["original_storage_key"]).read_bytes() == raw
    assert not list((case["root"] / ".uploads").iterdir())
    conn.commit()
    repeated = upload(api_client, case, raw)
    assert repeated.status_code == 200 and repeated.json()["data"] == {"materialId": material["id"], "jobId": job["id"]}
    assert len(conn.execute(select(t.ai_jobs)).all()) == 1


def test_real_user_lock_rejects_competing_upload_then_retry_reuses(api_client, material_case, domain_connection):
    from app.db.session import engine
    case, conn, raw = material_case, domain_connection, pdf_bytes()
    if conn.dialect.name == "sqlite":
        with engine.connect() as request_connection:
            request_connection.exec_driver_sql("PRAGMA busy_timeout=50")
    lock_user(conn, UUID(case["user"]))
    try:
        blocked = upload(api_client, case, raw)
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "REQUEST_IN_PROGRESS", blocked.text
    finally:
        conn.rollback()
    assert_no_admission(conn, case)
    first = upload(api_client, case, raw)
    again = upload(api_client, case, raw)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert len(conn.execute(select(t.materials)).all()) == len(conn.execute(select(t.ai_jobs)).all()) == 1


def test_cancelled_multipart_stream_closes_partial_spool():
    async def exercise():
        async def chunks():
            yield (b'--test\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
                   b'Content-Type: application/pdf\r\n\r\n%PDF-1.7\n')
            raise asyncio.CancelledError
        parser = storage.LimitedPdfParser(Headers({"content-type": "multipart/form-data; boundary=test"}), chunks(), limit=25)
        with pytest.raises(asyncio.CancelledError):
            await parser.parse()
        assert parser._files_to_close_on_error and all(file.closed for file in parser._files_to_close_on_error)
    asyncio.run(exercise())


def test_incomplete_multipart_is_rejected_and_spool_is_closed(api_client, material_case, domain_connection, monkeypatch):
    import starlette.formparsers as forms
    original, opened = forms.SpooledTemporaryFile, []
    def track(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr(forms, "SpooledTemporaryFile", track)
    raw = (b'--test\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
           b'Content-Type: application/pdf\r\n\r\n%PDF-1.7\n')
    response = api_client.post("/api/materials", headers={**material_case["headers"],
        "Content-Type": "multipart/form-data; boundary=test"}, content=raw)
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_INPUT"
    assert opened and all(file.closed for file in opened)
    assert_no_admission(domain_connection, material_case)
