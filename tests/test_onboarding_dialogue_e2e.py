"""HTTP + durable DB turns; only the external model boundary is a fixture."""
from copy import deepcopy
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.ai.gateway import GatewayResult
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.onboarding import GOAL_COLUMNS
from app.repositories.ai_results import AiResultRepository
from app.repositories.locking import lock_user

TODAY = date(2026, 11, 16)
FIELDS = ['examDate', 'goal', 'goalTotal', 'goalEtc']
TEXT = '시험은 2026-11-20, 목표 A+, 총 목표 없음, 기타 없음'


def interpreted(context, *, confirmed=True):
    item = {'subjectId': context['subjects'][0]['id'], 'values': {key: None for key in GOAL_COLUMNS},
        'providedFields': ['exam_date', 'target_grade', 'target_score'],
        'confirmedFields': {field: confirmed for field in FIELDS},
        'confirmationEvidence': {field: context['text'] if confirmed else None for field in FIELDS}}
    item['values'].update(exam_date='2026-11-20', target_grade='A+', target_score=90)
    return {'message': '네 칸을 확인했어요' if confirmed else '네 칸을 각각 확인해 주세요', 'chips': [],
            'isConfirmed': True, 'goals': [item], 'event': None}


class ModelFixture:
    def __init__(self, engine):
        self.engine, self.calls, self.transform, self.callback, self.fail = engine, 0, None, None, False

    def run(self, request, **_):
        import json
        self.calls += 1
        # This separate connection can take the user lock because interpretation
        # is outside the API's auth/capture transaction.
        with self.engine.begin() as conn:
            assert lock_user(conn, UUID(request.owner_id)) is not None
        if self.callback:
            self.callback()
        if self.fail:
            raise ApiError('AI_FAILED', '검증된 AI 결과를 준비하지 못했어요', 503)
        context = json.loads(request.identity.validation_json)
        value = interpreted(context)
        if self.transform:
            value = self.transform(value, context)
        saved = AiResultRepository(self.engine).save(request, value)
        return GatewayResult(value, saved, 'live', 1)


@pytest.fixture
def dialogue_case(api_client, domain_connection, monkeypatch):
    from app.routers import session
    from app.services import onboarding_dialogue
    from app.db.session import engine
    monkeypatch.setattr(session, 'DEMO_MODE', False)
    user = api_client.post('/api/session').json()['data']['anonymousId']
    headers = {'X-Anonymous-Id': user}
    subject = api_client.post('/api/subjects', headers=headers, json={'name': '자료구조'}).json()['data']['subjectId']
    domain_connection.execute(update(t.users).where(t.users.c.id == user).values(demo_date=TODAY))
    domain_connection.commit()
    model = ModelFixture(engine)
    monkeypatch.setattr(onboarding_dialogue, 'AiGateway', lambda *_args, **_kwargs: model)
    return {'user': user, 'subject': subject, 'headers': headers, 'model': model}


def message(client, case, *, text=TEXT, key='turn-1', subject=None):
    body = {'text': text}
    if subject is not None:
        body['subjectId'] = subject
    return client.post('/api/onboarding/message', headers={**case['headers'], **({'Idempotency-Key': key} if key is not None else {})}, json=body)


def test_empty_account_goal_dialogue_and_response_loss_retry(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    before = api_client.get('/api/onboarding', headers=case['headers']).json()['data']
    assert not before['done'] and not any(before['subjects'][0]['confirmedFields'].values())
    first = message(api_client, case)
    assert first.status_code == 200, first.text
    assert first.json()['data']['done']
    assert first.json()['data']['subjects'][0]['goalTotal'] is None
    assert first.json()['data']['subjects'][0]['confirmedFields'] == dict.fromkeys(FIELDS, True)
    assert len(first.json()['data']['messages']) == 2
    conn.execute(update(t.users).where(t.users.c.id == case['user']).values(onboarding_status='completed'))
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['subject']).values(status='active'))
    conn.commit()
    replay = message(api_client, case)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert case['model'].calls == 1
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 2
    conn.commit()
    conflict = message(api_client, case, text='다른 내용')
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_tentative_values_do_not_confirm_four_fields(api_client, dialogue_case):
    case = dialogue_case
    case['model'].transform = lambda value, context: interpreted(context, confirmed=False)
    response = message(api_client, case)
    assert response.status_code == 200, response.text
    subject = response.json()['data']['subjects'][0]
    assert subject['examDate'] == '2026-11-20' and subject['goal'] == 'A+'
    assert not response.json()['data']['done'] and not any(subject['confirmedFields'].values())
    case['model'].transform = None
    confirmed = message(api_client, case, key='turn-2')
    assert confirmed.status_code == 200 and confirmed.json()['data']['done']


def test_ambiguous_turn_only_asks_question_and_preserves_goal(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    case['model'].transform = lambda value, context: {'message': '시험 날짜를 정확히 알려 주세요', 'chips': [],
        'isConfirmed': False, 'goals': [], 'event': None}
    response = message(api_client, case, text='다음달쯤 시험인데')
    assert response.status_code == 200 and not response.json()['data']['done'], response.text
    assert conn.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id == case['subject'])) is None
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 2


@pytest.mark.parametrize('bad', ['foreign', 'missing_evidence', 'past_date', 'false_confirmed', 'unknown_field'])
def test_invalid_model_output_never_mutates_goals(api_client, domain_connection, dialogue_case, bad):
    case, conn = dialogue_case, domain_connection
    def transform(value, context):
        item = value['goals'][0]
        if bad == 'foreign':
            item['subjectId'] = str(uuid4())
        elif bad == 'missing_evidence':
            item['confirmationEvidence']['goalEtc'] = 'not in user text'
        elif bad == 'past_date':
            item['values']['exam_date'] = '2026-01-01'
        elif bad == 'false_confirmed':
            value['isConfirmed'] = False
        else:
            item['providedFields'].append('user_id')
        return value
    case['model'].transform = transform
    response = message(api_client, case)
    assert response.status_code == 503 and response.json()['error']['code'] == 'AI_FAILED', response.text
    assert conn.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id == case['subject'])) is None
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0
    assert conn.scalar(select(t.ai_jobs.c.status)) == 'failed'


def test_provider_failure_can_retry_same_key_without_duplicate_messages(api_client, dialogue_case):
    case = dialogue_case
    case['model'].fail = True
    response = message(api_client, case)
    assert response.status_code == 503
    case['model'].fail = False
    response = message(api_client, case)
    assert response.status_code == 200 and len(response.json()['data']['messages']) == 2
    assert case['model'].calls == 2


def test_goal_edit_while_interpreting_is_stale(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    def edit():
        with case['model'].engine.begin() as writer:
            writer.execute(update(t.subjects).where(t.subjects.c.id == case['subject']).values(target_grade='B+'))
    case['model'].callback = edit
    response = message(api_client, case)
    assert response.status_code == 409 and response.json()['error']['code'] == 'INPUT_STALE', response.text
    assert conn.scalar(select(t.subjects.c.target_grade).where(t.subjects.c.id == case['subject'])) == 'B+'
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0


def test_foreign_or_active_subject_is_rejected_before_model(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    response = message(api_client, case, subject=str(uuid4()))
    assert response.status_code == 404
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['subject']).values(status='active'))
    conn.commit()
    response = message(api_client, case, subject=case['subject'])
    assert response.status_code == 409 and response.json()['error']['code'] == 'ONBOARDING_COMPLETED'
    assert case['model'].calls == 0


def test_two_identical_unkeyed_answers_remain_distinct_turns(api_client, dialogue_case):
    case = dialogue_case
    case['model'].transform = lambda value, context: {'message': '다음 정보도 알려 주세요', 'chips': [],
        'isConfirmed': False, 'goals': [], 'event': None}
    assert message(api_client, case, text='없음', key=None).status_code == 200
    second = message(api_client, case, text='없음', key=None)
    assert second.status_code == 200 and len(second.json()['data']['messages']) == 4


def test_unconfigured_real_gateway_never_confirms_goals(api_client, domain_connection, dialogue_case, monkeypatch):
    from app.ai.gateway import AiGateway
    from app.services import onboarding_dialogue
    case, conn = dialogue_case, domain_connection
    monkeypatch.setattr(onboarding_dialogue, 'AiGateway', AiGateway)
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    response = message(api_client, case)
    assert response.status_code == 503 and response.json()['error']['code'] == 'AI_FAILED', response.text
    assert conn.scalar(select(t.subjects.c.target_score).where(t.subjects.c.id == case['subject'])) is None
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0


def test_message_write_failure_rolls_back_goals_and_retry_is_atomic(api_client, domain_connection, dialogue_case, monkeypatch):
    from app.repositories.onboarding import OnboardingRepository
    case, conn = dialogue_case, domain_connection
    original = OnboardingRepository.append_messages
    def fail_after_append(self, *args):
        original(self, *args)
        raise RuntimeError('response serialization failed')
    monkeypatch.setattr(OnboardingRepository, 'append_messages', fail_after_append)
    failed = message(api_client, case)
    assert failed.status_code == 500
    assert conn.scalar(select(t.subjects.c.target_score).where(t.subjects.c.id == case['subject'])) is None
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0
    conn.commit()
    monkeypatch.setattr(OnboardingRepository, 'append_messages', original)
    retried = message(api_client, case)
    assert retried.status_code == 200 and len(retried.json()['data']['messages']) == 2
