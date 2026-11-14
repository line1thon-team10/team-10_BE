"""Elapsed real time only constrains fresh allocation, never stored preview TTL."""
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import update
from app.db import schema as t
from app.domain.scheduling import free_slots
from app.services.allocator import allocate
from app.services.plan_inputs import initial_allocation_input, existing_allocation_input
from tests.test_plan_draft import TODAY, NOW, plan_case
from tests.test_plan_addition_e2e import addition_case


def test_real_day_initial_input_clips_elapsed_local_minutes_exactly(domain_connection,plan_case):
    conn,case=domain_connection,plan_case
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=None))
    at=datetime(2026,11,16,5,23,30,tzinfo=timezone.utc)
    request=initial_allocation_input(conn,case['user'],today=TODAY,timezone_name='Asia/Seoul',at=at)
    slots=free_slots(TODAY,request.busy,request.fixed)
    assert slots[0].start==Fraction(1727,2) # 14:23:30 local
    result=allocate(request)
    assert result.feasible
    assert all(task.day>TODAY or task.start>=Fraction(1727,2) for task in result.placements)


@pytest.mark.parametrize('zone,at,expected',[('America/New_York',datetime(2026,11,1,17,tzinfo=timezone.utc),720),
    ('America/New_York',datetime(2026,3,8,16,tzinfo=timezone.utc),720)])
def test_dst_wall_clock_projection_does_not_use_fixed_offset(domain_connection,plan_case,zone,at,expected):
    conn,case=domain_connection,plan_case;day=at.astimezone(ZoneInfo(zone)).date()
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=None,timezone=zone))
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['subject']).values(exam_date=day+timedelta(days=3)))
    request=initial_allocation_input(conn,case['user'],today=day,timezone_name=zone,at=at)
    assert free_slots(day,request.busy)[0].start==expected


def test_demo_date_retains_full_virtual_day(domain_connection,plan_case):
    request=initial_allocation_input(domain_connection,plan_case['user'],today=TODAY,timezone_name='Asia/Seoul',
        at=datetime(2026,11,16,14,tzinfo=timezone.utc))
    assert free_slots(TODAY,request.busy)[0].start==480


def test_past_fixed_fact_does_not_conflict_with_elapsed_gap(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case;task=case['old_task']
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=None))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(status='completed',scheduled_date=TODAY,
        starts_at=datetime(2026,11,16,0,tzinfo=timezone.utc),ends_at=datetime(2026,11,16,1,tzinfo=timezone.utc)))
    request,_,_=existing_allocation_input(conn,case['user'],today=TODAY,timezone_name='Asia/Seoul',
        subject_ids=(case['added'],),at=datetime(2026,11,16,6,tzinfo=timezone.utc))
    fixed=next(row for row in request.fixed if row.id==task['id'])
    assert (fixed.start,fixed.end)==(540,600)
    assert all(not (busy.day==fixed.day and busy.start<fixed.end and busy.end>fixed.start) for busy in request.busy)
    result=allocate(request)
    assert result.feasible
    assert all(row.day>TODAY or row.start>=900 for row in result.placements)
