"""Independent subprocess checks; never invokes Docker or an external provider."""
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_sigterm_during_lost_claim_never_starts_next_candidate(tmp_path):
    marker = tmp_path / 'calls'
    script = '''
from pathlib import Path
import os
import signal
import sys
from uuid import uuid4
from sqlalchemy import create_engine
from app.db import session
from app.db import schema as t
from app.core.clock import now_utc
from app.services.material_analysis import MaterialAnalysisService
from app.workers.materials import main
engine = create_engine('sqlite://')
t.metadata.create_all(engine)
now, user = now_utc(), str(uuid4())
with engine.begin() as conn:
    conn.execute(t.users.insert().values(id=user, created_at=now, updated_at=now))
    for number in range(2):
        conn.execute(t.ai_jobs.insert().values(id=str(uuid4()), user_id=user, kind='pdf_analysis',
            status='queued', target_type='material', target_id=str(uuid4()), idempotency_key=str(number),
            input_hash=str(number), input_payload={}, schema_version='test', created_at=now))
session.engine = engine
session.check_db_revision = lambda: None
class Worker(MaterialAnalysisService):
    def __init__(self, engine):
        super().__init__(engine)
        self.calls = 0
    def run_job(self, user_id, job_id):
        self.calls += 1
        with Path(sys.argv[1]).open('a') as out:
            out.write(str(self.calls) + '\\n')
        if self.calls == 1:
            os.kill(os.getpid(), signal.SIGTERM)
            return False
        return True
raise SystemExit(main(['--loop', '--health-file', sys.argv[2]], service_factory=Worker))
'''
    result = subprocess.run([sys.executable, '-c', script, str(marker), str(tmp_path / 'health.json')],
        cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == '1\n', 'Worker accepted another candidate after SIGTERM'

