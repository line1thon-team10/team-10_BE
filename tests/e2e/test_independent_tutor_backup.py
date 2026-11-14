"""Independent backup API must use only exact owned saved answers."""
from sqlalchemy import func, select

from app.ai.gateway import AiGateway as RealGateway
from app.db import schema as t
from tests.test_material_storage import material_case
from tests.test_tutor_e2e import tutor_case
from tests.test_tutor_backup_e2e import backup, fail_message_write


def test_backup_uses_latest_failed_question_without_provider_or_owner_leak(api_client, tutor_case, domain_connection, monkeypatch):
    from app.services import tutor
    case, conn = tutor_case, domain_connection
    fail_message_write(api_client, case)
    logs = conn.scalar(select(func.count()).select_from(t.ai_usage_logs))
    conn.commit()
    calls = []
    class BackupOnlyGateway:
        def __init__(self, engine): self.real = RealGateway(engine)
        def run(self, request, *, validator, provider, mode):
            assert mode == 'backup' and provider is None
            calls.append(mode)
            return self.real.run(request, validator=validator, provider=provider, mode=mode)
    monkeypatch.setattr(tutor, 'AiGateway', BackupOnlyGateway)
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    assert backup(api_client, case, user=case['other_user']).status_code == 503
    assert calls == []
    restored = backup(api_client, case)
    assert restored.status_code == 200 and restored.json()['data']['saved'] is True, restored.text
    assert calls == ['backup']
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == logs
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 1
