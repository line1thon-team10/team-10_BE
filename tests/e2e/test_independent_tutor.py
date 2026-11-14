"""HTTP, physical PDF, pinned prompt, gateway and DB tutor boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, Lock

import pymupdf
import pytest
from sqlalchemy import func, select, update

from app.ai.gateway import AiGateway as RealGateway, FunctionRef, ProviderSpec
from app.db import schema as t
from tests.test_material_storage import material_case, upload, pdf_bytes
from tests.test_tutor_e2e import tutor_case, ask, history_of
from tests.test_tutor_backup_e2e import backup


def multi_pdf(*pages):
    with pymupdf.open() as document:
        for text in pages:
            document.new_page().insert_text((20, 30), text)
        return document.tobytes()


def test_real_prompt_maps_ranked_page_back_to_physical_pdf_and_easy_does_not_expand(api_client, tutor_case, domain_connection, monkeypatch, tmp_path):
    from app.services import tutor
    case, conn = tutor_case, domain_connection
    second = upload(api_client, case, multi_pdf('Second PDF introduction.', 'ZEBRA_TOKEN graph invariant source.'))
    assert second.status_code == 200, second.text
    second_id = second.json()['data']['materialId']
    foreign = upload(api_client, case, pdf_bytes('FOREIGN_SECRET ZEBRA_TOKEN'), subject=case['other_subject'],
        headers={'X-Anonymous-Id': case['other_user']})
    assert foreign.status_code == 200, foreign.text
    prompt_file = tmp_path / 'captured-prompt.json'
    monkeypatch.setenv('INDEPENDENT_TUTOR_PROMPT', str(prompt_file))
    monkeypatch.setattr(tutor, 'PROVIDER', ProviderSpec(FunctionRef('tests.e2e.independent_tutor_transport', 'provider')))
    original = ask(api_client, case, 'Explain ZEBRA_TOKEN graph invariant')
    assert original.status_code == 200, original.text
    data = original.json()['data']
    assert data['pages'] == [{'materialId': second_id, 'from': 2, 'to': 2}]
    prompt = json.loads(prompt_file.read_text())
    assert 'ZEBRA_TOKEN' in prompt['source_pages'][0]['text']
    assert 'FOREIGN_SECRET' not in json.dumps(prompt)
    assert prompt['question'] == 'Explain ZEBRA_TOKEN graph invariant' and prompt['history'] == []
    easy = ask(api_client, case, 'Explain simply', history_of(original))
    assert easy.status_code == 200, easy.text
    assert easy.json()['data']['parentId'] == data['id']
    assert easy.json()['data']['pages'] == data['pages']
    simplified_prompt = json.loads(prompt_file.read_text())
    assert len(simplified_prompt['source_pages']) == 1
    assert simplified_prompt['source_pages'][0]['text'] == data['answer']
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 2


@pytest.mark.parametrize('change', ['version', 'physical_file'])
def test_source_change_after_gateway_prevents_late_answer(api_client, tutor_case, domain_connection, monkeypatch, change):
    from app.services import tutor
    case, conn = tutor_case, domain_connection
    class Gateway:
        def __init__(self, engine): self.real, self.engine = RealGateway(engine), engine
        def run(self, *args, **kwargs):
            outcome = self.real.run(*args, **kwargs)
            if change == 'version':
                with self.engine.begin() as writer:
                    writer.execute(update(t.materials).where(t.materials.c.id == case['materialId']).values(analysis_version='new-version'))
            else:
                with self.engine.connect() as reader:
                    key = reader.scalar(select(t.materials.c.original_storage_key).where(t.materials.c.id == case['materialId']))
                (case['root'] / key).write_bytes(pdf_bytes('Changed PDF after generation.'))
            return outcome
    monkeypatch.setattr(tutor, 'AiGateway', Gateway)
    response = ask(api_client, case)
    assert response.status_code == 503, response.text
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 0
    assert conn.scalar(select(func.count()).select_from(t.tutor_sources)) == 0
    conn.commit()
    assert backup(api_client, case).status_code == 503


@pytest.mark.parametrize('easy', [False, True])
def test_two_identical_http_completions_persist_one_message(api_client, tutor_case, domain_connection, monkeypatch, easy):
    from app.services import tutor
    case, conn = tutor_case, domain_connection
    parent = ask(api_client, case) if easy else None
    if parent is not None: assert parent.status_code == 200, parent.text
    history = history_of(parent) if parent is not None else None
    entered, release, lock = Event(), Event(), Lock()
    calls = 0
    class Gateway:
        def __init__(self, engine): self.real = RealGateway(engine)
        def run(self, *args, **kwargs):
            nonlocal calls
            outcome = self.real.run(*args, **kwargs)
            with lock:
                calls += 1
                first = calls == 1
            if first:
                entered.set()
                assert release.wait(10)
            return outcome
    monkeypatch.setattr(tutor, 'AiGateway', Gateway)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(ask, api_client, case, 'Simplify' if easy else 'Explain AVL rotations', history)
        try:
            assert entered.wait(10)
            second = ask(api_client, case, 'Simplify' if easy else 'Explain AVL rotations', history)
            assert second.status_code == 200, second.text
        finally:
            release.set()
        first = first.result(timeout=10)
    assert first.status_code == 200, first.text
    assert first.json()['data']['id'] == second.json()['data']['id']
    assert first.json()['data']['saved'] is True
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == (2 if easy else 1)
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == (2 if easy else 1)

