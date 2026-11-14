"""Later uncertainty must not silently re-confirm or erase unrelated goals."""
from uuid import uuid4

from sqlalchemy import select

from app.db import schema as t
from tests.test_onboarding_dialogue_e2e import dialogue_case, message, interpreted


def test_tentative_changed_date_clears_only_its_previous_confirmation(api_client, dialogue_case):
    case = dialogue_case
    confirmed = message(api_client, case)
    assert confirmed.status_code == 200 and confirmed.json()['data']['done']
    def tentative(output, context):
        value = interpreted(context, confirmed=False)
        value['goals'][0]['values']['exam_date'] = '2026-11-22'
        return value
    case['model'].transform = tentative
    response = message(api_client, case, text='시험이 2026-11-22일 수도 있는데 아직 확정은 아니야', key=str(uuid4()))
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert not data['done']
    subject = data['subjects'][0]
    assert subject['examDate'] == '2026-11-22'
    assert subject['confirmedFields'] == {'examDate': False, 'goal': True, 'goalTotal': True, 'goalEtc': True}


def test_ambiguous_question_preserves_prior_goal_values_and_confirmations(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    confirmed = message(api_client, case)
    assert confirmed.status_code == 200 and confirmed.json()['data']['done']
    before = dict(conn.execute(select(t.subjects).where(t.subjects.c.id == case['subject'])).mappings().one())
    conn.commit()
    case['model'].transform = lambda *_: {'message': '어떤 정보를 변경하시겠어요?', 'chips': [],
        'isConfirmed': False, 'goals': [], 'event': None}
    response = message(api_client, case, text='아직 잘 모르겠어', key=str(uuid4()))
    assert response.status_code == 200, response.text
    assert dict(conn.execute(select(t.subjects).where(t.subjects.c.id == case['subject'])).mappings().one()) == before
    assert len(response.json()['data']['messages']) == 4
