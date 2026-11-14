"""Durable material analysis: short fenced transactions around bounded AI work."""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.ai.gateway import AiGateway, GatewayRequest, MaterialRevision, ResultIdentity
from app.ai.material_adapter import (MODEL_VERSION, PROMPT_VERSION, SCHEMA_VERSION, PROVIDER, VALIDATOR,
                                     validate_material_result)
from app.ai.settings import AiSettings
from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.materials import MaterialRepository
from app.services.material_storage import MaterialStorageService

STEPS = ['파일 확인', '자료 분석', '결과 저장']
ENVELOPE = 'material-job-v1'


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class MaterialAnalysisService:
    provider, validator = PROVIDER, VALIDATOR

    def __init__(self, engine, *, storage_dir=None, gateway=None, clock=now_utc):
        self.engine, self.clock = engine, clock
        self.settings = AiSettings.from_environment()
        self.gateway = gateway or AiGateway(engine, settings=self.settings)
        self.storage = MaterialStorageService(engine, storage_dir=storage_dir)

    @staticmethod
    def _job(conn, user_id, job_id):
        row = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.id == job_id,
            t.ai_jobs.c.user_id == user_id, t.ai_jobs.c.kind == 'pdf_analysis',
            t.ai_jobs.c.target_type == 'material')).mappings().first()
        if row is None:
            raise ApiError('NO_JOB', '분석 작업을 찾지 못했어요', 404)
        return dict(row)

    def _expired(self, job, now):
        envelope = job['result'] or {}
        try:
            expires = datetime.fromisoformat(envelope['leaseExpiresAt'])
        except (KeyError, TypeError, ValueError):
            started = job['started_at']
            return started is None or _aware(started) + timedelta(seconds=self.settings.pdf_job_budget_seconds + 10) <= now
        return _aware(expires) <= now

    def view(self, user_id, job_id):
        with self.engine.connect() as conn:
            job = self._job(conn, user_id, job_id)
            material = MaterialRepository(conn).material(user_id, job['target_id'])
        value = job['result'] or {}
        status = 'done' if job['status'] in ('done', 'succeeded') else 'failed' if job['status'] in ('failed', 'cancelled') else 'running'
        current = value.get('current', 0)
        if type(current) is not int or not 0 <= current < len(STEPS):
            current = 0
        return {'jobId': job_id, 'materialId': material['id'], 'subjectId': material['subject_id'],
            'status': status, 'ocr': value.get('ocr'), 'steps': STEPS, 'current': current,
            'failedStep': current if status == 'failed' else None,
            'progress': 100 if status == 'done' else min(99, current * 33),
            'reused': value.get('reused', False),
            'reason': value.get('reason') if status in ('done', 'failed') else None}

    def _claim(self, user_id, job_id):
        now = self.clock()
        try:
            with self.engine.begin() as conn:
                if lock_user(conn, UUID(user_id)) is None:
                    return None
                job = self._job(conn, user_id, job_id)
                if job['status'] != 'queued' and not (job['status'] == 'running' and self._expired(job, now)):
                    return None
                token, generation = str(uuid4()), job['retry_count'] + 1
                envelope = {'schemaVersion': ENVELOPE, 'leaseToken': token,
                    'leaseExpiresAt': (now + timedelta(seconds=self.settings.pdf_job_budget_seconds + 10)).isoformat(),
                    'current': 0, 'ocr': None, 'reused': False, 'reason': None}
                changed = conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job_id,
                    t.ai_jobs.c.status == job['status'], t.ai_jobs.c.retry_count == job['retry_count']).values(
                    status='running', stage='reading', started_at=now, finished_at=None,
                    retry_count=generation, result=envelope, error_code=None))
                if changed.rowcount != 1:
                    return None
                job.update(status='running', retry_count=generation, result=envelope, started_at=now)
                return job
        except UserBusyError:
            return None

    def _owned_lease(self, conn, claim):
        if lock_user(conn, UUID(claim['user_id'])) is None:
            return None
        try:
            job = self._job(conn, claim['user_id'], claim['id'])
        except ApiError:
            return None
        if (job['status'] != 'running' or job['retry_count'] != claim['retry_count']
                or (job['result'] or {}).get('leaseToken') != claim['result']['leaseToken']
                or self._expired(job, self.clock())):
            return None
        return job

    def _current_material(self, conn, job):
        repo, payload = MaterialRepository(conn), job['input_payload']
        material = repo.material(job['user_id'], job['target_id'])
        latest = repo.latest_job(job['user_id'], job['target_id'])
        if (payload.get('schemaVersion') != 'material-analysis-input-v1'
                or payload.get('materialId') != material['id']
                or payload.get('subjectId') != material['subject_id']
                or payload.get('sourceSha256') != material['sha256']
                or payload.get('analysisVersion') != material['analysis_version']
                or payload.get('structureHash') != repo.structure_hash(job['user_id'], material['id'])
                or latest is None or latest['id'] != job['id']):
            raise ApiError('MATERIAL_STALE', '분석 중 자료가 변경됐어요. 다시 업로드해 주세요', 409)
        return material

    def _prepare(self, claim):
        with self.engine.begin() as conn:
            job = self._owned_lease(conn, claim)
            if job is None:
                return None
            material = self._current_material(conn, job)
        path, _ = self.storage.original_file(claim['user_id'], material['id'])
        with self.engine.begin() as conn:
            job = self._owned_lease(conn, claim)
            if job is None:
                return None
            material = self._current_material(conn, job)
            envelope = {**job['result'], 'current': 1}
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job['id']).values(stage='analyzing', result=envelope))
            conn.execute(update(t.materials).where(t.materials.c.id == material['id']).values(
                analysis_status='running', analysis_stage='analyzing', error_message=None, updated_at=self.clock()))
        context = {'sourceSha256': material['sha256'], 'analysisVersion': material['analysis_version'],
                   'totalPages': material['page_count']}
        identity = ResultIdentity.create(kind='pdf_analysis', provider='groq-upstage', model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION, input={'path': str(path), **context},
            validation_context=context, materials=(MaterialRevision(material['id'],
                material['sha256'] + ':' + material['analysis_version']),))
        return GatewayRequest(claim['user_id'], identity, job_id=claim['id'], pdf_job=True), context

    def _mark_saving(self, claim):
        with self.engine.begin() as conn:
            job = self._owned_lease(conn, claim)
            if job is None:
                return False
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job['id']).values(
                stage='saving', result={**job['result'], 'current': 2}))
        return True

    def _finish(self, claim, outcome, context):
        result = validate_material_result(outcome.data, context)
        now = self.clock()
        with self.engine.begin() as conn:
            job = self._owned_lease(conn, claim)
            if job is None:
                return False
            material = self._current_material(conn, job)
            # The old structures may have learning/quiz history: reanalysis is
            # informational and must never resurrect, retire, or overwrite them.
            has_structure = conn.scalar(select(t.units.c.id).where(t.units.c.material_id == material['id']).limit(1)) is not None
            if not has_structure:
                for index, unit in enumerate(result['units'], 1):
                    unit_id = str(uuid4())
                    conn.execute(t.units.insert().values(id=unit_id, material_id=material['id'],
                        code=f'unit_{index:02d}', title=unit['title'], sort_order=index,
                        start_page=unit['start_page'], end_page=unit['end_page'], difficulty=unit['difficulty'],
                        evidence=unit['evidence'], created_at=now, updated_at=now))
                    for number, concept in enumerate(unit['concepts'], 1):
                        conn.execute(t.concepts.insert().values(id=str(uuid4()), unit_id=unit_id,
                            code=f'concept_{number:02d}', title=concept['title'], sort_order=number,
                            start_page=concept['start_page'], end_page=concept['end_page'],
                            created_at=now, updated_at=now))
            quality = result['quality']
            conn.execute(update(t.materials).where(t.materials.c.id == material['id']).values(
                analysis_status='succeeded', analysis_stage='done', extraction_method=result['method'],
                quiz_enabled=quality['quiz_allowed'], quiz_disabled_reason=quality['reason'],
                error_message=None, updated_at=now))
            envelope = {**job['result'], 'current': 2, 'ocr': result['method'] == 'ocr',
                'reused': outcome.source != 'live', 'reason': None if quality['quiz_allowed'] else quality['message'],
                'analysis': result, 'structurePreserved': has_structure}
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job['id']).values(
                status='succeeded', stage='done', result=envelope, saved_result_id=outcome.saved_result_id,
                finished_at=now, error_code=None))
        return True

    def _fail(self, claim, error):
        with self.engine.begin() as conn:
            job = self._owned_lease(conn, claim)
            if job is None:
                return False
            stale = isinstance(error, ApiError) and error.code == 'MATERIAL_STALE'
            message = ('분석 중 자료가 변경됐어요. 다시 업로드해 주세요' if stale else
                       '자료 분석을 완료하지 못했어요. 다시 업로드하거나 목차를 직접 입력해 주세요')
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job['id']).values(
                status='failed', finished_at=self.clock(), error_code='MATERIAL_STALE' if stale else 'AI_FAILED',
                result={**job['result'], 'reason': message}))
            # A stale worker must not change the material status of a newer job.
            latest = MaterialRepository(conn).latest_job(job['user_id'], job['target_id'])
            if latest and latest['id'] == job['id'] and not stale:
                conn.execute(update(t.materials).where(t.materials.c.id == job['target_id']).values(
                    analysis_status='failed', error_message=message, updated_at=self.clock()))
        return True

    def run_job(self, user_id, job_id):
        claim = self._claim(user_id, job_id)
        if claim is None:
            return False
        from app.ai.gateway import CleanupFailed
        try:
            prepared = self._prepare(claim)
            if prepared is None:
                return False
            request, context = prepared
            outcome = self.gateway.run(request, validator=self.validator, provider=self.provider)
            if not self._mark_saving(claim):
                return False
            return self._finish(claim, outcome, context)
        except CleanupFailed:
            raise
        except Exception as error:
            return self._fail(claim, error)

    def run_next(self, *, stop_requested=lambda: False):
        with self.engine.connect() as conn:
            candidates = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.kind == 'pdf_analysis',
                t.ai_jobs.c.target_type == 'material', t.ai_jobs.c.status.in_(('queued', 'running')))
                .order_by(t.ai_jobs.c.created_at, t.ai_jobs.c.id)).mappings().all()
        for job in candidates:
            if stop_requested():
                return False
            if job['status'] == 'running' and not self._expired(job, self.clock()):
                continue
            if self.run_job(job['user_id'], job['id']):
                return True
        from app.services.question_bank import QuestionBankService
        return QuestionBankService(self.engine, storage_dir=self.storage.root,
            gateway=self.gateway, clock=self.clock).run_next(stop_requested=stop_requested)
