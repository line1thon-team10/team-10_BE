"""Durable preparation after PDF analysis, independent from quiz GET requests."""
import hashlib
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.ai.gateway import GatewayRequest, MaterialRevision, ResultIdentity, canonical_json
from app.ai.question_bank_adapter import (MODEL_VERSION, PROMPT_VERSION, SCHEMA_VERSION, PROVIDER, VALIDATOR,
                                          evidence_for, validate_question_bank)
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.materials import MaterialRepository
from app.services.material_analysis import MaterialAnalysisService


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class QuestionBankService(MaterialAnalysisService):
    # Reuse the material worker's short claim/lease/finalization transactions.
    # A bank failure affects only this job, never successful PDF analysis.
    provider, validator = PROVIDER, VALIDATOR

    @staticmethod
    def _job(conn, user_id, job_id):
        row = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.id == job_id,
            t.ai_jobs.c.user_id == user_id, t.ai_jobs.c.kind == 'question_pregenerate',
            t.ai_jobs.c.target_type == 'unit')).mappings().first()
        if row is None:
            raise ApiError('NO_JOB', '문항 준비 작업을 찾지 못했어요', 404)
        return dict(row)

    def _snapshot(self, conn, user_id, unit_id, difficulty):
        unit = conn.execute(select(t.units).join(t.materials, t.units.c.material_id == t.materials.c.id)
            .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id).where(t.units.c.id == unit_id,
                t.subjects.c.user_id == user_id, t.units.c.in_scope.is_(True), t.units.c.retired_at.is_(None))).mappings().first()
        if unit is None or not unit['evidence']:
            raise ApiError('QUESTION_BANK_STALE', '문항 준비 범위가 변경됐어요', 409)
        repo = MaterialRepository(conn)
        material = repo.material(user_id, unit['material_id'])
        analysis = repo.latest_job(user_id, material['id'])
        result = (analysis['result'] or {}).get('analysis') if analysis else None
        if (material['analysis_status'] != 'succeeded' or not material['quiz_enabled']
                or material['original_deleted_at'] is not None or not analysis or analysis['status'] != 'succeeded'
                or not result or result['sourceSha256'] != material['sha256']
                or result['analysisVersion'] != material['analysis_version']
                or result['totalPages'] != material['page_count']):
            raise ApiError('QUESTION_BANK_STALE', '원문 분석 결과가 변경됐어요', 409)
        concepts = [{str(key): value for key, value in row.items()} for row in conn.execute(select(t.concepts.c.id, t.concepts.c.title,
            t.concepts.c.start_page, t.concepts.c.end_page).where(t.concepts.c.unit_id == unit_id,
                t.concepts.c.in_scope.is_(True), t.concepts.c.retired_at.is_(None))
                .order_by(t.concepts.c.sort_order, t.concepts.c.id)).mappings()]
        if (not concepts or unit['start_page'] is None or unit['end_page'] is None
                or any(not unit['start_page'] <= concept['start_page'] <= concept['end_page'] <= unit['end_page']
                       <= material['page_count'] for concept in concepts)):
            raise ApiError('QUESTION_BANK_STALE', '문항을 준비할 개념 범위가 없어요', 409)
        return {'unitId': unit_id, 'materialId': material['id'], 'analysisJobId': analysis['id'],
            'sourceSha256': material['sha256'], 'analysisVersion': material['analysis_version'],
            'totalPages': material['page_count'], 'structureHash': repo.structure_hash(user_id, material['id']),
            'difficulty': difficulty, 'unit': {'title': unit['title'], 'concepts': concepts},
            'pages': [page for page in result['pages'] if unit['start_page'] <= page['page'] <= unit['end_page']]}

    def _discover(self, stop_requested):
        with self.engine.connect() as conn:
            units = conn.execute(select(t.units.c.id, t.subjects.c.user_id)
                .join(t.materials, t.units.c.material_id == t.materials.c.id)
                .join(t.subjects, t.subjects.c.id == t.materials.c.subject_id)
                .where(t.materials.c.analysis_status == 'succeeded', t.materials.c.quiz_enabled.is_(True),
                       t.units.c.in_scope.is_(True), t.units.c.retired_at.is_(None))
                .order_by(t.materials.c.created_at, t.units.c.sort_order, t.units.c.id)).all()
        # Prioritize the first-round bank for every unit before advanced banks.
        for difficulty in ('low', 'mid', 'high'):
            for unit_id, user_id in units:
                if stop_requested():
                    return None
                try:
                    with self.engine.begin() as conn:
                        if lock_user(conn, UUID(user_id)) is None:
                            continue
                        payload = self._snapshot(conn, user_id, unit_id, difficulty)
                        fingerprint = digest(payload)
                        key = f'question-bank:{unit_id}:{difficulty}:{fingerprint}'
                        if conn.scalar(select(t.ai_jobs.c.id).where(t.ai_jobs.c.user_id == user_id,
                                t.ai_jobs.c.idempotency_key == key)):
                            continue
                        job_id = str(uuid4())
                        conn.execute(t.ai_jobs.insert().values(id=job_id, user_id=user_id,
                            kind='question_pregenerate', target_type='unit', target_id=unit_id,
                            status='queued', stage='queued', idempotency_key=key, input_hash=fingerprint,
                            input_payload=payload, schema_version=SCHEMA_VERSION, created_at=self.clock()))
                        return user_id, job_id
                except (UserBusyError, ApiError):
                    continue
        return None

    def _current(self, conn, job):
        current = self._snapshot(conn, job['user_id'], job['target_id'], job['input_payload']['difficulty'])
        if digest(current) != job['input_hash'] or current != job['input_payload']:
            raise ApiError('QUESTION_BANK_STALE', '문항 준비 중 원문이나 범위가 변경됐어요', 409)
        return current

    def _prepare(self, claim):
        with self.engine.begin() as conn:
            job = self._owned_lease(conn, claim)
            if job is None:
                return None
            payload = self._current(conn, job)
        path, _ = self.storage.original_file(claim['user_id'], payload['materialId'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != payload['sourceSha256']:
            raise ApiError('QUESTION_BANK_STALE', '문항 준비 원본 PDF가 변경됐어요', 409)
        identity = ResultIdentity.create(kind='question_pregenerate', provider='groq', model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION, input=payload, validation_context=payload,
            materials=(MaterialRevision(payload['materialId'], payload['sourceSha256'] + ':' + payload['analysisVersion']),))
        return GatewayRequest(claim['user_id'], identity, job_id=claim['id']), payload

    def _finish(self, claim, outcome, context):
        value = validate_question_bank(outcome.data, context)
        sources = {item['id']: item for item in evidence_for(context)}
        now = self.clock()
        with self.engine.begin() as conn:
            job = self._owned_lease(conn, claim)
            if job is None:
                return False
            self._current(conn, job)
            for question in value['questions']:
                question_id = str(uuid4())
                conn.execute(t.questions.insert().values(id=question_id, unit_id=context['unitId'],
                    concept_id=question['concept_id'], code=f"bank_{job['id']}_{question['id']:02d}",
                    type={'multiple_choice': 'choice', 'short_answer': 'short', 'essay': 'essay'}[question['type']],
                    difficulty=context['difficulty'], prompt=question['question'],
                    choices=[{'text': option} for option in question['options']], correct_answer=question['answer'],
                    explanation=question['explanation'], origin='pregenerated', generation_version=PROMPT_VERSION,
                    created_at=now))
                source = sources[question['evidence_id']]
                conn.execute(t.question_sources.insert().values(id=str(uuid4()), question_id=question_id,
                    material_id=context['materialId'], start_page=source['page'], end_page=source['page'], quote=source['text']))
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job['id']).values(status='succeeded',
                stage='done', saved_result_id=outcome.saved_result_id, finished_at=now, error_code=None,
                result={**job['result'], 'count': len(value['questions']), 'reused': outcome.source != 'live'}))
        return True

    def _fail(self, claim, error):
        with self.engine.begin() as conn:
            job = self._owned_lease(conn, claim)
            if job is None:
                return False
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job['id']).values(status='failed',
                finished_at=self.clock(), error_code='QUESTION_BANK_STALE' if isinstance(error, ApiError)
                    and error.code == 'QUESTION_BANK_STALE' else 'AI_FAILED',
                result={**job['result'], 'reason': '문항 준비를 완료하지 못했어요'}))
        return True

    def retry(self, user_id, job_id):
        """Explicit operational retry; polling never retries a failed provider."""
        with self.engine.begin() as conn:
            if lock_user(conn, UUID(user_id)) is None:
                raise ApiError('NO_JOB', '문항 준비 작업을 찾지 못했어요', 404)
            job = self._job(conn, user_id, job_id)
            if job['status'] != 'failed':
                return False
            self._current(conn, job)
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == job_id).values(status='queued',
                stage='queued', finished_at=None, error_code=None))
        return True

    def run_next(self, *, stop_requested=lambda: False):
        with self.engine.connect() as conn:
            candidates = [dict(row) for row in conn.execute(select(t.ai_jobs).where(
                t.ai_jobs.c.kind == 'question_pregenerate', t.ai_jobs.c.target_type == 'unit',
                t.ai_jobs.c.status.in_(('queued', 'running')))
                .order_by(t.ai_jobs.c.created_at, t.ai_jobs.c.id)).mappings()]
        for job in candidates:
            if stop_requested():
                return False
            if job['status'] == 'running' and not self._expired(job, self.clock()):
                continue
            if self.run_job(job['user_id'], job['id']):
                return True
        admitted = self._discover(stop_requested)
        return self.run_job(*admitted) if admitted and not stop_requested() else False
