"""A literal quote must support the interpreted exam date and target."""
import pytest
from sqlalchemy import func, select

from app.db import schema as t
from tests.test_onboarding_dialogue_e2e import dialogue_case, message


@pytest.mark.parametrize('field,value', [('exam_date', '2026-11-21'), ('target_grade', 'B+')])
def test_confirmed_value_cannot_contradict_exact_source_quote(api_client, domain_connection, dialogue_case, field, value):
    case, conn = dialogue_case, domain_connection
    def transform(output, context):
        output['goals'][0]['values'][field] = value
        return output
    case['model'].transform = transform
    response = message(api_client, case)
    assert response.status_code == 503 and response.json()['error']['code'] == 'AI_FAILED', response.text
    assert conn.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id == case['subject'])) is None
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0
