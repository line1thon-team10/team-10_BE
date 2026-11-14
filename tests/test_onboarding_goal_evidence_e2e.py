"""Evidence for one goal cell must not confirm or replace another one."""
import pytest
from sqlalchemy import func, select

from app.db import schema as t
from tests.test_onboarding_dialogue_e2e import dialogue_case, message


@pytest.mark.parametrize('text,mutation', [
    ('시험은 2026-11-20이고 목표는 A+', {}),
    ('시험은 2026-11-20, 목표 A+, 총 목표 없음, 기타 없음', {'exam_date': '2026-11-21'}),
    ('시험은 2026-11-20, 목표 A+, 총 목표 없음, 기타 없음', {'target_grade': 'B+', 'target_score': 80}),
    ('시험은 2026-11-20, 목표 A+, 총 목표 없음, 기타 없음', {'target_score': 99}),
    ('시험은 2026-11-20, 목표 A+, 총 목표는 있어, 기타 없음', {}),
    ('시험은 2026-11-20, 목표 A+, 총 목표 모르겠어, 기타 없음', {}),
    ('시험은 2026-11-20, 목표 A+, 총 목표 없는 건 아니야, 기타 없음', {}),
])
def test_unsubstantiated_confirmation_is_atomic_failure(api_client, domain_connection, dialogue_case, text, mutation):
    case = dialogue_case
    def transform(value, context):
        value['goals'][0]['values'].update(mutation)
        return value
    case['model'].transform = transform
    response = message(api_client, case, text=text)
    assert response.status_code == 503, response.text
    assert response.json()['error']['code'] == 'AI_FAILED'
    assert domain_connection.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id == case['subject'])) is None
    assert domain_connection.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0


@pytest.mark.parametrize('text', [
    '시험은 2026년 11월 20일, 목표 A+, 총 목표 없음, 기타 없음',
    '시험은 11월 20일, 목표 A+ 90점, 커트라인 없음, 반영 비율 없음',
])
def test_equivalent_explicit_goal_evidence_is_supported(api_client, dialogue_case, text):
    response = message(api_client, dialogue_case, text=text)
    assert response.status_code == 200, response.text
    assert response.json()['data']['done']


@pytest.mark.parametrize('question,expected', [
    ('기타 정보를 알려 주세요', 200),
    ('총 목표와 기타 정보를 알려 주세요', 503),
    ('시험 날짜를 알려 주세요', 503),
])
def test_short_absence_answer_only_confirms_the_single_questioned_group(api_client, dialogue_case, question, expected):
    case = dialogue_case
    case['model'].transform = lambda *_: {'message': question, 'chips': [], 'isConfirmed': False, 'goals': [], 'event': None}
    assert message(api_client, case, text='질문해 줘').status_code == 200
    def transform(value, context):
        item = value['goals'][0]
        item['values'] = dict.fromkeys(item['values'])
        item['providedFields'] = []
        item['confirmedFields'] = {field: field == 'goalEtc' for field in item['confirmedFields']}
        item['confirmationEvidence'] = {field: '없음' if field == 'goalEtc' else None for field in item['confirmedFields']}
        return value
    case['model'].transform = transform
    response = message(api_client, case, text='없음', key='22222222-2222-4222-8222-222222222222')
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()['data']['subjects'][0]['confirmedFields']['goalEtc']
        assert not response.json()['data']['done']
