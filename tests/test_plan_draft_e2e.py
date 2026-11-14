"""Real HTTP/DB plan preparation, approval, replay and input invalidation."""
from datetime import timedelta
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.db import schema as t
from app.domain.scheduling import SearchLimit
from tests.test_plan_draft import NOW, TODAY, plan_case


def headers(case, foreign=False):
    return {'X-Anonymous-Id': case['other_user' if foreign else 'user']}


def draft(client, case, **params):
    return client.get('/api/plan/draft', headers=headers(case), params=params)


def approve(client, case, preview, foreign=False):
    return client.post('/api/plan/approve', headers=headers(case, foreign), json={'previewId': preview})


def test_draft_approve_replay_and_persisted_exact_ranges(api_client, domain_connection, plan_case):
    case, conn = plan_case, domain_connection
    response = draft(api_client, case)
    assert response.status_code == 200, response.text
    value = response.json()['data']
    preview = value['previewId']
    assert value['summary']['totalPages'] == 20
    assert value['summary']['daysToFirstExam'] == 3
    assert len(value['firstWeek']) == 7
    assert all(isinstance(day['minutes'], dict) for day in value['firstWeek'])
    assert conn.scalar(select(func.count()).select_from(t.plans)) == 0
    assert conn.scalar(select(func.count()).select_from(t.study_tasks)) == 0
    conn.commit()
    repeated = draft(api_client, case)
    assert repeated.status_code == 200 and repeated.json() == response.json()
    result = approve(api_client, case, preview)
    assert result.status_code == 200 and result.json()['data'] == {'approved': True}, result.text
    plan = conn.execute(select(t.plans)).mappings().one()
    tasks = [dict(row) for row in conn.execute(select(t.study_tasks)).mappings()]
    ranges = [dict(row) for row in conn.execute(select(t.study_task_ranges)).mappings()]
    assert len(tasks) == value['summary']['taskCount']
    assert all(task['plan_id'] == plan['id'] and 15 <= task['estimated_minutes'] <= 60 for task in tasks)
    assert all(TODAY <= task['scheduled_date'] < TODAY + timedelta(days=3) for task in tasks)
    assert sorted(page for row in ranges for page in range(row['start_page'], row['end_page'] + 1)) == list(range(1, 21))
    changes = conn.scalar(select(t.plan_proposals.c.changes).where(t.plan_proposals.c.id == preview))
    assert {row['id'] for row in changes['placements']} == {task['id'] for task in tasks}
    assert all(Fraction(*part['baseline']) == Fraction(5, 2) and Fraction(*part['multiplier']) == 1
               for task in changes['placements'] for part in task['parts'])
    subject = conn.execute(select(t.subjects).where(t.subjects.c.id == case['subject'])).mappings().one()
    assert subject['status'] == 'active'
    conn.execute(update(t.users).where(t.users.c.id == case['user']).values(demo_date=TODAY + timedelta(days=1)))
    conn.commit()
    assert approve(api_client, case, preview).json() == result.json()
    assert [dict(row) for row in conn.execute(select(t.study_tasks)).mappings()] == tasks
    assert conn.scalar(select(t.plans.c.revision)) == 1


@pytest.mark.parametrize('mutation', ['date', 'target', 'scope', 'concept', 'schedule'])
def test_draft_changes_require_new_candidate_without_any_plan_writes(api_client, domain_connection, plan_case, mutation):
    case, conn = plan_case, domain_connection
    preview = draft(api_client, case).json()['data']['previewId']
    if mutation == 'date':
        conn.execute(update(t.users).where(t.users.c.id == case['user']).values(demo_date=TODAY + timedelta(days=1)))
    elif mutation == 'target':
        conn.execute(update(t.subjects).where(t.subjects.c.id == case['subject']).values(target_score=80))
    elif mutation == 'scope':
        conn.execute(update(t.units).where(t.units.c.id == case['unit']).values(end_page=19))
    elif mutation == 'concept':
        conn.execute(t.concepts.insert().values(id=str(uuid4()), unit_id=case['unit'], code='new', title='new',
            sort_order=1, start_page=1, end_page=18, created_at=NOW, updated_at=NOW))
    else:
        conn.execute(t.calendar_events.insert().values(id=str(uuid4()), user_id=case['user'], event_type='other', title='busy',
            starts_at=NOW, ends_at=NOW + timedelta(hours=1), created_at=NOW, updated_at=NOW))
    conn.commit()
    result = approve(api_client, case, preview)
    assert result.status_code == 409 and result.json()['error']['code'] == 'PREVIEW_STALE', result.text
    assert conn.scalar(select(func.count()).select_from(t.plans)) == 0
    assert conn.scalar(select(func.count()).select_from(t.study_tasks)) == 0
    conn.commit()
    replacement = draft(api_client, case)
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()['data']['previewId'] != preview


def test_foreign_candidate_and_selection_are_rejected(api_client, domain_connection, plan_case):
    case, conn = plan_case, domain_connection
    preview = draft(api_client, case).json()['data']['previewId']
    assert approve(api_client, case, preview, foreign=True).status_code == 404
    for value in [case['foreign'], case['active'], str(uuid4()), f"{case['subject']},{case['subject']}", '']:
        result = draft(api_client, case, draftSubjectIds=value)
        assert result.status_code == 422, result.text
    assert conn.scalar(select(func.count()).select_from(t.plans)) == 0


def test_search_exhaustion_is_distinct_from_infeasibility(api_client, domain_connection, plan_case, monkeypatch):
    from app.services import plan_draft
    def limited(request):
        raise SearchLimit(1, 2)
    monkeypatch.setattr(plan_draft, 'allocate', limited)
    response = draft(api_client, plan_case)
    assert response.status_code == 503 and response.json()['error']['code'] == 'PLAN_SEARCH_LIMIT', response.text
    assert domain_connection.scalar(select(func.count()).select_from(t.plan_proposals)) == 0


def test_infeasible_short_scope_never_stores_a_candidate(api_client, domain_connection, plan_case):
    conn, case = domain_connection, plan_case
    conn.execute(update(t.units).where(t.units.c.id == case['unit']).values(end_page=1))
    conn.commit()
    response = draft(api_client, case)
    assert response.status_code == 409 and response.json()['error']['code'] == 'PLAN_INFEASIBLE', response.text
    assert conn.scalar(select(func.count()).select_from(t.plan_proposals)) == 0


def test_only_selected_subjects_are_activated(api_client, domain_connection, plan_case):
    case, conn = plan_case, domain_connection
    row = dict(conn.execute(select(t.subjects).where(t.subjects.c.id == case['subject'])).mappings().one())
    extra = str(uuid4())
    conn.execute(t.subjects.insert().values(**{**row, 'id': extra, 'code': 'extra'}))
    conn.commit()
    response = draft(api_client, case, draftSubjectIds=case['subject'])
    assert response.status_code == 200, response.text
    assert approve(api_client, case, response.json()['data']['previewId']).status_code == 200
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id == extra)) == 'draft'
