"""Owned single-turn tutor messages with durable failed-request identities."""
from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.ai.gateway import AiGateway, CleanupFailed, GatewayRequest, MaterialRevision, ResultIdentity, canonical_json
from app.ai.material_adapter import MODEL_VERSION
from app.ai.tutor_adapter import PROMPT_VERSION, SCHEMA_VERSION, PROVIDER, VALIDATOR, validate_tutor_result
from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import UserBusyError, lock_user
from app.services.material_storage import MaterialStorageService


def failed():
    return ApiError('AI_FAILED', '자료에서 확인된 튜터 답변을 준비하지 못했어요. 다시 시도해 주세요', 503)


def _lock(conn, user_id):
    try:
        if lock_user(conn, UUID(user_id)) is None:
            raise ApiError('UNKNOWN_SESSION', '사용자를 찾지 못했어요', 401)
    except UserBusyError:
        raise ApiError('REQUEST_IN_PROGRESS', '다른 요청이 처리 중이에요. 잠시 후 다시 시도해 주세요', 409) from None


def _pages(values):
    return sorted([{'materialId': row['materialId'], 'from': row['from'], 'to': row['to']} for row in values],
                  key=lambda row: (row['materialId'], row['from'], row['to']))


class TutorService:
    def __init__(self, engine, *, gateway=None, storage_dir=None):
        self.engine = engine
        self.gateway = gateway or AiGateway(engine)
        self.storage = MaterialStorageService(engine, storage_dir=storage_dir)

    def _sources(self, conn, message_id):
        return [{'materialId': row['material_id'], 'from': row['start_page'], 'to': row['end_page'], 'quote': row['quote']}
                for row in conn.execute(select(t.tutor_sources).where(t.tutor_sources.c.message_id == message_id)
                                        .order_by(t.tutor_sources.c.material_id, t.tutor_sources.c.start_page, t.tutor_sources.c.id)).mappings()]

    def _parent(self, conn, user_id, history):
        if not history:
            return None
        value = history[0].model_dump(by_alias=True)
        try:
            canonical_json(value)
        except ValueError:
            raise ApiError('BAD_REQUEST', '저장된 원문 문답을 보내 주세요', 400) from None
        matches = []
        rows = conn.execute(select(t.tutor_messages).where(t.tutor_messages.c.user_id == user_id,
            t.tutor_messages.c.kind == 'question', t.tutor_messages.c.question == value['question'],
            t.tutor_messages.c.answer == value['answer'])).mappings()
        for row in rows:
            sources = self._sources(conn, row['id'])
            if _pages(sources) == _pages(value['pages']):
                versions = None
                for job in conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.user_id == user_id,
                        t.ai_jobs.c.kind == 'tutor', t.ai_jobs.c.status == 'succeeded')).mappings():
                    if (job['result'] or {}).get('messageId') == row['id']:
                        versions = {item['id']: [item['sha256'], item['analysisVersion']]
                            for item in job['input_payload']['payload']['materials']
                            if item['id'] in {cite['materialId'] for cite in sources}}
                        break
                matches.append({**dict(row), 'sources': sources, 'versions': versions})
        if len(matches) > 1:
            current = {row['id']: [row['sha256'], row['analysis_version']]
                for row in conn.execute(select(t.materials).join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
                    .where(t.subjects.c.user_id == user_id)).mappings()}
            valid = [row for row in matches if row['versions'] is not None
                     and all(current.get(key) == value for key, value in row['versions'].items())]
            if len(valid) == 1:
                matches = valid
        if len(matches) != 1:
            raise ApiError('BAD_REQUEST', '본인의 저장된 원문 문답 한 쌍과 일치해야 해요', 400)
        parent = matches[0]
        return {'id': parent['id'], 'question': parent['question'], 'answer': parent['answer'],
            'subjectId': parent['subject_id'], 'pages': _pages(parent['sources']), 'sources': parent['sources'],
            'versions': parent['versions']}

    def _materials(self, conn, user_id, parent=None):
        query = select(t.materials, t.subjects.c.name.label('subject_name')).join(t.subjects,
            t.materials.c.subject_id == t.subjects.c.id).where(t.subjects.c.user_id == user_id)
        if parent is not None:
            query = query.where(t.materials.c.id.in_({row['materialId'] for row in parent['pages']}))
        materials = []
        for row in conn.execute(query.order_by(t.materials.c.id)).mappings():
            if type(row['page_count']) is not int or row['page_count'] < 1:
                continue
            pages = None
            # Only an owned validated analysis of this exact material version
            # supplies OCR text. Failed jobs and unrelated global caches cannot.
            jobs = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.user_id == user_id,
                t.ai_jobs.c.kind == 'pdf_analysis', t.ai_jobs.c.target_id == row['id'],
                t.ai_jobs.c.status == 'succeeded').order_by(t.ai_jobs.c.created_at.desc(), t.ai_jobs.c.id.desc())).mappings()
            for job in jobs:
                analysis = (job['result'] or {}).get('analysis')
                if (type(analysis) is dict and analysis.get('sourceSha256') == row['sha256']
                        and analysis.get('analysisVersion') == row['analysis_version']
                        and analysis.get('totalPages') == row['page_count'] and type(analysis.get('pages')) is list):
                    pages = analysis['pages']
                    break
            key = self.storage.storage_key(user_id, row['id'], row['sha256'])
            path = str(self.storage.root / key) if row['original_storage_key'] == key and row['original_deleted_at'] is None else None
            materials.append({'id': row['id'], 'subjectId': row['subject_id'], 'subjectName': row['subject_name'],
                'sha256': row['sha256'], 'analysisVersion': row['analysis_version'], 'pageCount': row['page_count'],
                'pages': pages, 'path': path})
        return materials

    @staticmethod
    def _identity(payload):
        return ResultIdentity.create(kind='tutor_simplify' if payload['parent'] else 'tutor', provider='groq',
            model_version=MODEL_VERSION, prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
            input=payload, validation_context=payload,
            materials=tuple(MaterialRevision(row['id'], row['sha256'] + ':' + row['analysisVersion']) for row in payload['materials']))

    def _request(self, user_id, body):
        with self.engine.begin() as conn:
            _lock(conn, user_id)
            parent = self._parent(conn, user_id, body.history)
            payload = {'question': parent['question'] if parent else body.question, 'parent': parent,
                       'materials': self._materials(conn, user_id, parent)}
            identity, identifier, now = self._identity(payload), str(uuid4()), now_utc()
            conn.execute(t.ai_jobs.insert().values(id=identifier, user_id=user_id, kind=identity.kind,
                status='running', stage='answering', target_type='tutor', target_id=parent['id'] if parent else None,
                idempotency_key='tutor:' + identifier, input_hash=identity.input_hash,
                input_payload={'schemaVersion': 'tutor-request-v1', 'payload': payload}, schema_version=SCHEMA_VERSION,
                created_at=now, started_at=now))
        return GatewayRequest(user_id, identity, job_id=identifier), payload

    def _check_current(self, conn, user_id, payload):
        if canonical_json(self._materials(conn, user_id, payload['parent'])) != canonical_json(payload['materials']):
            raise failed()
        if payload['parent']:
            parent = conn.execute(select(t.tutor_messages).where(t.tutor_messages.c.id == payload['parent']['id'],
                t.tutor_messages.c.user_id == user_id, t.tutor_messages.c.kind == 'question')).mappings().first()
            if parent is None or parent['question'] != payload['parent']['question'] or parent['answer'] != payload['parent']['answer']:
                raise failed()
            if self._sources(conn, parent['id']) != payload['parent']['sources']:
                raise failed()

    def _stored(self, conn, request):
        results = []
        for job in conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.user_id == request.owner_id,
            t.ai_jobs.c.kind == request.identity.kind, t.ai_jobs.c.input_hash == request.identity.input_hash,
            t.ai_jobs.c.status == 'succeeded').order_by(t.ai_jobs.c.created_at, t.ai_jobs.c.id)).mappings():
            value = job['result'] or {}
            response = value.get('response')
            if type(response) is dict and response.get('id') == value.get('messageId'):
                exists = conn.scalar(select(t.tutor_messages.c.id).where(t.tutor_messages.c.id == response['id'],
                                                                          t.tutor_messages.c.user_id == request.owner_id))
                if exists and response['id'] not in {row[0]['id'] for row in results}:
                    results.append((deepcopy(response), job['saved_result_id']))
        if len(results) > 1:
            raise failed()
        return results[0] if results else None

    def _complete(self, request, payload, outcome):
        result = validate_tutor_result(outcome.data, payload)
        with self.engine.begin() as conn:
            _lock(conn, request.owner_id)
            self._check_current(conn, request.owner_id, payload)
            job = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.id == request.job_id,
                t.ai_jobs.c.user_id == request.owner_id, t.ai_jobs.c.status == 'running')).mappings().first()
            if job is None:
                raise failed()
            previous = self._stored(conn, request)
            saved_result_id = outcome.saved_result_id
            if previous:
                response, saved_result_id = previous
                response['saved'] = True
            else:
                parent_id = payload['parent']['id'] if payload['parent'] else None
                if parent_id and conn.scalar(select(t.tutor_messages.c.id).where(t.tutor_messages.c.parent_message_id == parent_id)):
                    # One original has one easy explanation; do not overwrite a
                    # historical child when the current material basis differs.
                    raise failed()
                identifier, now = str(uuid4()), now_utc()
                subject = conn.execute(select(t.subjects).where(t.subjects.c.id == result['subjectId'],
                    t.subjects.c.user_id == request.owner_id)).mappings().first()
                if subject is None:
                    raise failed()
                response = {'id': identifier, 'subjectId': subject['id'], 'subjectName': subject['name'],
                    'question': payload['question'], 'answer': result['answer'].strip(), 'parentId': parent_id,
                    'pages': _pages(result['sources']), 'saved': outcome.source != 'live', 'mockFilled': []}
                conn.execute(t.tutor_messages.insert().values(id=identifier, user_id=request.owner_id,
                    subject_id=subject['id'], parent_message_id=parent_id, kind='simplify' if parent_id else 'question',
                    question=response['question'], answer=response['answer'], is_fallback=outcome.source != 'live',
                    created_at=now, answered_at=now))
                conn.execute(t.tutor_sources.insert(), [{'id': str(uuid4()), 'message_id': identifier,
                    'material_id': row['materialId'], 'start_page': row['from'], 'end_page': row['to'], 'quote': row['quote']}
                    for row in result['sources']])
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == request.job_id).values(status='succeeded',
                stage='done', result={'schemaVersion': SCHEMA_VERSION, 'messageId': response['id'], 'response': response},
                saved_result_id=saved_result_id, is_fallback=response['saved'], finished_at=now_utc(), error_code=None))
            return response

    def _failure(self, request):
        with self.engine.begin() as conn:
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == request.job_id,
                t.ai_jobs.c.user_id == request.owner_id, t.ai_jobs.c.status == 'running').values(
                status='failed', error_code='AI_FAILED', finished_at=now_utc()))

    def ask(self, user_id, body):
        request, payload = self._request(user_id, body)
        try:
            outcome = self.gateway.run(request, validator=VALIDATOR, provider=PROVIDER)
            return self._complete(request, payload, outcome)
        except CleanupFailed:
            self._failure(request)
            raise
        except Exception:
            self._failure(request)
            raise failed() from None
