"""Independent recovery HTTP edge checks."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.domain.scheduling import SearchLimit
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_replan_options_e2e import recovery_case, options, apply
from tests.test_replan_safety_e2e import snapshot


@pytest.mark.parametrize('mutation', ['date', 'recovery', 'calendar'])
def test_input_changes_during_computation_cannot_publish_a_preview(api_client, domain_connection, recovery_case, monkeypatch, mutation):
    from app.services import replan_options
    from app.db.session import engine
    c, conn = recovery_case, domain_connection
    original = replan_options.build_recovery_candidates
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with engine.begin() as other:
            if mutation == 'date':
                other.execute(update(t.users).where(t.users.c.id == c['user']).values(demo_date=TODAY+timedelta(days=1)))
            elif mutation == 'recovery':
                other.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == c['request']).values(status='stale'))
            else:
                other.execute(t.calendar_events.insert().values(id=str(uuid4()), user_id=c['user'], event_type='other', title='Changed while computing',
                    starts_at=NOW+timedelta(hours=1), ends_at=NOW+timedelta(hours=2), created_at=NOW, updated_at=NOW))
        return result
    monkeypatch.setattr(replan_options, 'build_recovery_candidates', changed)
    before = list(conn.scalars(select(t.plan_proposals.c.id))); conn.commit()
    response = options(api_client, c)
    assert response.status_code == 409 and response.json()['error']['code'] == 'PREVIEW_STALE', response.text
    assert list(conn.scalars(select(t.plan_proposals.c.id))) == before
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id == c['user'])) == 1


def test_search_exhaustion_is_retryable_and_does_not_persist_disabled_options(api_client, domain_connection, recovery_case, monkeypatch):
    from app.services import replan_options
    c, conn = recovery_case, domain_connection
    original = replan_options.build_recovery_candidates
    before = snapshot(conn); conn.commit()
    def exhausted(*args, **kwargs):
        raise SearchLimit(1, 2)
    monkeypatch.setattr(replan_options, 'build_recovery_candidates', exhausted)
    response = options(api_client, c)
    assert response.status_code == 503 and response.json()['error']['code'] == 'PLAN_SEARCH_LIMIT', response.text
    assert snapshot(conn) == before
    conn.commit()
    monkeypatch.setattr(replan_options, 'build_recovery_candidates', original)
    response = options(api_client, c)
    assert response.status_code == 200 and any(o['enabled'] for o in response.json()['data']['options']), response.text


