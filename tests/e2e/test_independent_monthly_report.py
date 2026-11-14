"""Independent H-02 checks for finalized plans and measured-time evidence."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select, update
from app.db import schema as t
from tests.e2e.test_monthly_report import world, focus, report
from tests.test_progress_contract import progress_world


def test_close_does_not_resurrect_previously_excluded_planned_minutes(api_client, world, end_focus):
    w = world
    unit = w.unit(1, 1000)
    w.task((unit, 1, 10), done=[(unit, 1, 10)], status='completed')
    w.task((unit, 11, 20), status='excluded')
    before = report(api_client, w)
    assert before['time']['plannedMinutes'] == 25
    end_focus(w.user)
    response = api_client.post('/api/demo/next-day', headers={'X-Anonymous-Id': w.user, 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 200, response.text
    after = report(api_client, w)
    assert after['achievement'] == before['achievement'] == {'plan': 10, 'done': 10, 'rate': 100}
    assert after['time']['plannedMinutes'] == 25


def test_paused_focus_uses_closed_segments_without_open_elapsed_time(api_client, world, monkeypatch, end_focus):
    w = world
    w.conn.commit()
    end_focus(w.user)
    unit = w.unit(1, 10)
    task = w.task((unit, 1, 10))
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    sid = focus(w, task, w.day, [(start, start + timedelta(seconds=35)),
        (start + timedelta(minutes=2), start + timedelta(minutes=2, seconds=35))], measured=999999)
    w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == sid).values(status='paused', ended_at=None))
    w.conn.execute(t.study_session_segments.insert().values(id=str(uuid4()), session_id=sid, sort_order=2,
        started_at=start + timedelta(minutes=3), ended_at=None))
    monkeypatch.setattr('app.services.monthly_report.now_utc', lambda: start + timedelta(hours=10))
    value = report(api_client, w)
    assert value['time']['measuredMinutes'] == 1
    assert value['studyDays'] == 1
    before = w.conn.execute(select(t.study_session_segments).where(t.study_session_segments.c.session_id == sid)).all()
    w.conn.commit()
    assert report(api_client, w) == value
    assert w.conn.execute(select(t.study_session_segments).where(t.study_session_segments.c.session_id == sid)).all() == before


def test_cross_month_distinct_page_share_does_not_consume_previous_month_pages(api_client, world):
    w = world
    unit = w.unit(1, 10)
    w.task((unit, 1, 10), done=[(unit, 1, 10)], status='completed', day=date(2026, 10, 31))
    w.task((unit, 1, 10), done=[(unit, 1, 7)], status='partial', day=date(2026, 11, 1))
    november = report(api_client, w)
    assert november['subjects'] == [{'subjectId': w.subject['id'], 'pages': 7, 'share': 100.0}]
    october = report(api_client, w, '2026-10')
    assert october['subjects'] == [{'subjectId': w.subject['id'], 'pages': 10, 'share': 100.0}]
