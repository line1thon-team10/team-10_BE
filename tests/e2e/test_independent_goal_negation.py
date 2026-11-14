"""A cherry-picked literal quote cannot override an explicit user correction."""
import pytest
from sqlalchemy import func, select

from app.db import schema as t
from tests.test_onboarding_dialogue_e2e import dialogue_case, message


@pytest.mark.parametrize('group,text,quote', [
    ('examDate', '시험은 2026-11-20이 아니라 2026-11-21, 목표 A+, 총 목표 없음, 기타 없음', '2026-11-20'),
    ('goal', '시험은 2026-11-20, 목표는 A+가 아니라 B+, 총 목표 없음, 기타 없음', 'A+'),
])
def test_negated_old_value_is_not_confirmed_from_its_substring(api_client, domain_connection, dialogue_case, group, text, quote):
    case, conn = dialogue_case, domain_connection
    def transform(output, context):
        output['goals'][0]['confirmationEvidence'][group] = quote
        return output
    case['model'].transform = transform
    response = message(api_client, case, text=text)
    assert response.status_code == 503 and response.json()['error']['code'] == 'AI_FAILED', response.text
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0
