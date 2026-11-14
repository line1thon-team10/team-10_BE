"""Short transactions around interpreted turns, with durable response replay."""
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update

from app.ai.gateway import AiGateway, GatewayRequest, InvalidOutput, ResultIdentity
from app.ai.onboarding_adapter import MODEL_VERSION, PROMPT_VERSION, SCHEMA_VERSION, PROVIDER, VALIDATOR, validate_onboarding_result
from app.ai.settings import AiSettings
from app.core.clock import now_utc, today_for
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.onboarding import ConversationMessage, GOAL_COLUMNS, GoalUpdate
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.proposals import canonical_hash
from app.services.onboarding import OnboardingService

ENVELOPE = 'onboarding-turn-v1'


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _snapshot(conn, user, command, mode):
    onboarding = OnboardingService(conn, user)
    if command.get('subjectId') is not None:
        onboarding._draft(command['subjectId'])
    elif user['onboarding_status'] == 'completed' or conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id == user['id'])):
        raise ApiError('ONBOARDING_COMPLETED', '추가할 draft 과목을 선택해 주세요', 409)
    state = onboarding.get(command.get('subjectId'))
    rows = [onboarding.repository.subject(row['id']) for row in state['subjects']]
    subjects = [{key: row[key] for key in ('id', 'name', *sorted(GOAL_COLUMNS))} for row in rows]
    # No secrets or provider configuration are included in the AI context.
    payload = jsonable_encoder({'mode': mode, 'text': command['text'], 'today': today_for(user).isoformat(),
        'timezone': user['timezone'], 'subjectId': command.get('subjectId'), 'subjects': subjects, 'state': state})
    versions = {'context': payload, 'subjectRows': jsonable_encoder(rows),
        'events': jsonable_encoder(onboarding.repository.events([row['id'] for row in rows], include_global=True)),
        'plan': jsonable_encoder([dict(row) for row in conn.execute(select(t.plans).where(t.plans.c.user_id == user['id'])).mappings()])}
    return payload, canonical_hash(versions)


class OnboardingDialogueService:
    def __init__(self, engine, *, gateway=None, clock=now_utc):
        self.engine, self.clock = engine, clock
        self.settings = AiSettings.from_environment()
        self.gateway = gateway or AiGateway(engine, settings=self.settings)

    @staticmethod
    def _lock(conn, user_id):
        try:
            user = lock_user(conn, UUID(user_id))
        except UserBusyError:
            raise ApiError('REQUEST_IN_PROGRESS', '다른 대화를 처리 중이에요. 잠시 후 다시 시도해 주세요', 409) from None
        if user is None:
            raise ApiError('UNKNOWN_SESSION', '사용자를 찾지 못했어요', 401)
        return user.to_mapping()

    def _claim(self, user_id, command, mode, key):
        input_hash = canonical_hash({'mode': mode, 'command': command})
        now = self.clock()
        with self.engine.begin() as conn:
            user = self._lock(conn, user_id)
            row = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.user_id == user_id,
                t.ai_jobs.c.idempotency_key == key)).mappings().first()
            job = dict(row) if row else None
            if job is not None:
                if job['input_hash'] != input_hash or job['kind'] != 'nl_parse':
                    raise ApiError('IDEMPOTENCY_CONFLICT', '같은 요청 키에 다른 내용을 보낼 수 없어요', 409)
                envelope = job['result'] or {}
                if job['status'] == 'succeeded' and envelope.get('schemaVersion') == ENVELOPE:
                    return None, envelope['response']
                if job['status'] == 'running' and job['started_at'] is not None and (
                    _aware(job['started_at']) + timedelta(seconds=self.settings.request_budget_seconds + 10) > now):
                    raise ApiError('REQUEST_IN_PROGRESS', '같은 내용을 처리 중이에요. 잠시 후 다시 시도해 주세요', 409)
            payload, basis = _snapshot(conn, user, command, mode)
            identifier, token = job['id'] if job else str(uuid4()), str(uuid4())
            generation = job['retry_count'] + 1 if job else 1
            envelope = {'schemaVersion': ENVELOPE, 'token': token, 'basis': basis}
            values = dict(status='running', stage='interpreting', input_payload={'command': command, 'context': payload},
                retry_count=generation, started_at=now, finished_at=None, error_code=None, result=envelope)
            if job:
                conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == identifier).values(**values))
            else:
                conn.execute(t.ai_jobs.insert().values(id=identifier, user_id=user_id, kind='nl_parse',
                    target_type='onboarding_' + mode, target_id=command.get('subjectId'), idempotency_key=key,
                    input_hash=input_hash, schema_version=SCHEMA_VERSION, created_at=now, **values))
            return {'id': identifier, 'userId': user_id, 'generation': generation, 'token': token,
                    'basis': basis, 'context': payload, 'command': command, 'mode': mode}, None

    def _owned(self, conn, claim):
        user = self._lock(conn, claim['userId'])
        job = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.id == claim['id'], t.ai_jobs.c.user_id == claim['userId'])).mappings().first()
        if (job is None or job['status'] != 'running' or job['retry_count'] != claim['generation']
                or (job['result'] or {}).get('token') != claim['token']):
            raise ApiError('INPUT_STALE', '이 요청의 처리 기준이 바뀌었어요. 다시 시도해 주세요', 409)
        if _aware(job['started_at']) + timedelta(seconds=self.settings.request_budget_seconds + 10) <= self.clock():
            raise ApiError('INPUT_STALE', '요청 처리 시간이 지났어요. 다시 시도해 주세요', 409)
        return user

    def _failed(self, claim, code):
        with self.engine.begin() as conn:
            conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == claim['id'], t.ai_jobs.c.user_id == claim['userId'],
                t.ai_jobs.c.status == 'running', t.ai_jobs.c.retry_count == claim['generation']).values(
                status='failed', stage='failed', error_code=code, finished_at=self.clock()))

    def run(self, user_id, command, *, mode='message', idempotency_key=None):
        if mode != 'message':
            raise ValueError('Invalid onboarding interpretation mode')
        if idempotency_key is not None and (not 1 <= len(idempotency_key) <= 128 or any(
                not 33 <= ord(char) <= 126 for char in idempotency_key)):
            raise ApiError('INVALID_INPUT', '요청 키는 공백 없는 ASCII 1~128자로 보내 주세요', 422)
        key = f'onboarding:{mode}:' + (idempotency_key or str(uuid4()))
        claim, replay = self._claim(user_id, command, mode, key)
        if claim is None:
            return replay
        identity = ResultIdentity.create(kind='nl_parse', provider='groq', model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
            input=claim['context'], validation_context=claim['context'])
        try:
            result = self.gateway.run(GatewayRequest(user_id, identity, job_id=claim['id']), validator=VALIDATOR, provider=PROVIDER)
            try:
                value = validate_onboarding_result(result.data, claim['context'])
            except InvalidOutput:
                raise ApiError('AI_FAILED', '검증된 해석을 준비하지 못했어요. 다시 입력해 주세요', 503) from None
            with self.engine.begin() as conn:
                user = self._owned(conn, claim)
                try:
                    _, current = _snapshot(conn, user, command, mode)
                except ApiError:
                    raise ApiError('INPUT_STALE', '해석하는 동안 온보딩 상태가 바뀌었어요. 다시 요청해 주세요', 409) from None
                if current != claim['basis']:
                    raise ApiError('INPUT_STALE', '해석하는 동안 입력이 바뀌었어요. 다시 요청해 주세요', 409)
                updates = []
                for item in value['goals']:
                    values = {field: item['values'][field] for field in item['providedFields']}
                    if values.get('exam_date') is not None:
                        values['exam_date'] = date.fromisoformat(values['exam_date'])
                    # False means "not confirmed by this turn", not a command
                    # to clear earlier confirmations in unrelated groups.
                    updates.append(GoalUpdate(item['subjectId'], values,
                        {group: True for group, confirmed in item['confirmedFields'].items() if confirmed}))
                response = OnboardingService(conn, user).save_turn(subject_id=command.get('subjectId'), goals=updates,
                    messages=[ConversationMessage('user', command['text']), ConversationMessage('ai', value['message'], tuple(value['chips']))])
                response = jsonable_encoder(response)
                conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.id == claim['id'], t.ai_jobs.c.retry_count == claim['generation']).values(
                    status='succeeded', stage='done', finished_at=self.clock(), saved_result_id=result.saved_result_id,
                    result={'schemaVersion': ENVELOPE, 'token': claim['token'], 'basis': claim['basis'], 'response': response}))
                return response
        except Exception as error:
            self._failed(claim, error.code if isinstance(error, ApiError) else 'AI_FAILED')
            raise
