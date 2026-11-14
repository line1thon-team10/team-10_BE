"""Independent approval, historical evidence, DST and follow-up boundaries."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_plan_draft import plan_case, TODAY, NOW
from tests.test_plan_addition_e2e import addition_case
from tests.e2e.test_schedule_voice import voice_case, preview as voice_preview, apply as voice_apply
from tests.e2e.test_schedule_edit import calendar_world, preview as edit_preview, apply as edit_apply
from tests.e2e.test_schedule_delete import user, preview as delete_preview, apply as delete_apply
from tests.test_onboarding_events import add_event, at


def test_voice_moves_only_remaining_pages_and_keeps_recorded_session_facts(api_client, domain_connection, voice_case):
    case, conn = voice_case, domain_connection
    task = case['old_task']
    record, session = str(uuid4()), str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task['id']).values(scheduled_date=TODAY,
        starts_at=at(TODAY, '15:00'), ends_at=at(TODAY, '15:50'), status='partial'))
    conn.execute(t.study_sessions.insert().values(id=session, user_id=case['user'], task_id=task['id'], status='ended',
        started_at=at(TODAY, '15:00'), ended_at=at(TODAY, '15:12'), measured_seconds=720, study_date=TODAY, created_at=NOW))
    conn.execute(t.study_records.insert().values(id=record, task_id=task['id'], session_id=session, result='partial',
        study_date=TODAY, recorded_at=NOW, updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record, unit_id=case['unit'], start_page=1, end_page=5))
    tables = (t.study_records, t.study_record_ranges, t.study_sessions)
    facts = [conn.execute(select(table)).all() for table in tables]
    tasks = conn.execute(select(t.study_tasks)).all()
    revision = conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id == case['user']))
    conn.commit()
    response = voice_preview(api_client, case)
    assert response.status_code == 200, response.text
    candidate = response.json()['data']
    moved = next(row for row in candidate['moves'] if row['taskId'] == task['id'])
    assert sorted(page for piece in moved['pieces'] for page in range(piece['from'], piece['to'] + 1)) == list(range(6, 21))
    assert conn.execute(select(t.study_tasks)).all() == tasks
    assert conn.execute(select(t.calendar_events)).all() == []
    conn.commit()
    foreign = api_client.post('/api/schedules/voice/apply', headers={'X-Anonymous-Id': case['other_user']}, json={'previewId': candidate['previewId']})
    assert foreign.status_code == 404, foreign.text
    applied = voice_apply(api_client, case, candidate['previewId'])
    assert applied.status_code == 200, applied.text
    assert [conn.execute(select(table)).all() for table in tables] == facts
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id == case['user'])) == revision + 1
    rows = conn.execute(select(t.study_task_ranges).join(t.study_tasks, t.study_tasks.c.id == t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.subject_id == case['subject'], t.study_tasks.c.status == 'planned')).mappings().all()
    assert sorted(page for row in rows for page in range(row['start_page'], row['end_page'] + 1)) == list(range(6, 21))
    conn.commit()
    assert voice_apply(api_client, case, candidate['previewId']).json() == applied.json()


@pytest.mark.parametrize('target,start,end', [(date(2027, 3, 14), '02:30', '03:30'), (date(2027, 11, 7), '01:30', '02:30')])
def test_voice_and_edit_refuse_nonexistent_or_ambiguous_dst_instants(api_client, domain_connection, voice_case, target, start, end):
    case, conn = voice_case, domain_connection
    today = target-timedelta(days=1)
    conn.execute(update(t.users).where(t.users.c.id == case['user']).values(timezone='America/New_York', demo_date=today))
    owner = dict(conn.execute(select(t.users).where(t.users.c.id == case['user'])).mappings().one())
    original = add_event(conn, owner, day=today, start='12:00', end='13:00')
    before = conn.execute(select(t.calendar_events)).all()
    conn.commit()
    case['model'].value['event'].update(date=target.isoformat(), start=start, end=end)
    voice = voice_preview(api_client, case, f'{target} {start}부터 {end}까지 약속')
    assert voice.status_code == 400 and voice.json()['error']['code'] == 'PARSE_FAILED', voice.text
    edit = edit_preview(api_client, case['user'], f"{original['id']}@{today}", {'date': target.isoformat(), 'start': start, 'end': end})
    assert edit.status_code == 422, edit.text
    assert conn.execute(select(t.calendar_events)).all() == before
    assert conn.execute(select(t.plan_proposals).where(t.plan_proposals.c.proposal_type == 'schedule')).all() == []


def test_elapsed_hours_do_not_expire_edit_but_other_occurrence_cannot_apply_it(api_client, calendar_world, monkeypatch):
    from app.services import schedule_changes
    from app.services.proposals import ProposalService
    world = calendar_world
    original = add_event(world.conn, user(world), day=world.day, start='12:00', end='13:00')
    other = add_event(world.conn, user(world), day=world.day, start='18:00', end='19:00')
    world.conn.commit()
    clock = [datetime(2026, 11, 9, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(schedule_changes, 'ProposalService', lambda conn: ProposalService(conn, clock=lambda: clock[0]))
    key = f"{original['id']}@{world.day}"
    candidate = edit_preview(api_client, world.user, key, {'date': world.day.isoformat(), 'start': '14:00', 'end': '15:00'})
    assert candidate.status_code == 200, candidate.text
    identifier = candidate.json()['data']['previewId']
    wrong = edit_apply(api_client, world.user, f"{other['id']}@{world.day}", identifier)
    assert wrong.status_code == 404, wrong.text
    clock[0] += timedelta(hours=10)
    applied = edit_apply(api_client, world.user, key, identifier)
    assert applied.status_code == 200, applied.text
    assert edit_apply(api_client, world.user, key, identifier).json() == applied.json()


def test_delete_replay_does_not_reissue_consumed_followup_empty_time(api_client, calendar_world):
    world = calendar_world
    original = add_event(world.conn, user(world), day=world.day, start='12:00', end='13:00')
    key = f"{original['id']}@{world.day}"
    facts = [world.conn.execute(select(table)).all() for table in (t.study_tasks, t.study_records)]
    world.conn.commit()
    candidate = delete_preview(api_client, world, key)
    assert candidate.status_code == 200, candidate.text
    applied = delete_apply(api_client, world, key, candidate.json()['data']['previewId'])
    assert applied.status_code == 200, applied.text
    child = applied.json()['data']['previewId']
    assert child and child != candidate.json()['data']['previewId']
    rest = api_client.post('/api/plan/empty-time', headers={'X-Anonymous-Id': world.user}, json={'previewId': child, 'option': 'rest'})
    assert rest.status_code == 200, rest.text
    proposals = world.conn.execute(select(t.plan_proposals)).all()
    revision = world.conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id == world.user))
    world.conn.commit()
    assert delete_apply(api_client, world, key, candidate.json()['data']['previewId']).json() == applied.json()
    assert world.conn.execute(select(t.plan_proposals)).all() == proposals
    assert world.conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id == world.user)) == revision
    assert [world.conn.execute(select(table)).all() for table in (t.study_tasks, t.study_records)] == facts
