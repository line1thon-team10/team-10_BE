"""Calendar candidates affect occupancy without replacing stored facts."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select
from app.db import schema as t
from app.services.plan_inputs import PlanInputError, existing_allocation_input
from tests.test_plan_draft import TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case
from tests.test_onboarding_events import add_event, at


def prepare(conn,case,**options):
    return existing_allocation_input(conn,case['user'],today=TODAY,timezone_name='Asia/Seoul',
        subject_ids=(),affected_subject_ids=(case['subject'],),**options)[0]


def test_event_replace_remove_and_new_candidate_do_not_write(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    user=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    event=add_event(conn,user,day=TODAY,start='09:00',end='10:00')
    before=conn.execute(select(t.calendar_events)).all()
    candidate={**event,'starts_at':at(TODAY,'11:00'),'ends_at':at(TODAY,'12:00')}
    request=prepare(conn,case,calendar_event_overrides={event['id']:candidate})
    assert [(x.day,x.start,x.end) for x in request.busy]==[(TODAY,660,720)]
    assert prepare(conn,case,calendar_event_overrides={event['id']:None}).busy==()
    new_id=str(uuid4())
    request=prepare(conn,case,calendar_event_overrides={new_id:{**event,'id':new_id}})
    assert len(request.busy)==2
    assert conn.execute(select(t.calendar_events)).all()==before


def test_occurrence_override_preserves_original_date_and_moves_into_horizon(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    user=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    original=TODAY-timedelta(days=7)
    event=add_event(conn,user,day=original,recurrence_rule='FREQ=WEEKLY;BYDAY=MO')
    moved={'id':str(uuid4()),'event_id':event['id'],'occurrence_date':original,'kind':'moved',
        'new_starts_at':at(TODAY,'13:00'),'new_ends_at':at(TODAY,'14:00')}
    cancelled={'id':str(uuid4()),'event_id':event['id'],'occurrence_date':TODAY,'kind':'cancelled',
        'new_starts_at':None,'new_ends_at':None}
    request=prepare(conn,case,calendar_exception_overrides={event['id']:[moved,cancelled]})
    assert [(x.day,x.start,x.end) for x in request.busy]==[(TODAY,780,840)]
    assert conn.execute(select(t.calendar_event_exceptions)).first() is None


@pytest.mark.parametrize('bad',['foreign_event','foreign_subject','mismatched_id','unknown_remove','foreign_exception'])
def test_calendar_candidate_owner_boundaries(api_client,domain_connection,addition_case,bad):
    conn,case=domain_connection,addition_case
    user=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    other=dict(conn.execute(select(t.users).where(t.users.c.id==case['other_user'])).mappings().one())
    event=add_event(conn,user,day=TODAY)
    foreign=add_event(conn,other,day=TODAY)
    options={'calendar_event_overrides':{event['id']:{**event}}}
    if bad=='foreign_event': options={'calendar_event_overrides':{foreign['id']:{**foreign,'user_id':user['id']}}}
    if bad=='foreign_subject': options['calendar_event_overrides'][event['id']]['subject_id']=case['foreign']
    if bad=='mismatched_id': options['calendar_event_overrides'][event['id']]['id']=str(uuid4())
    if bad=='unknown_remove': options={'calendar_event_overrides':{str(uuid4()):None}}
    if bad=='foreign_exception': options={'calendar_exception_overrides':{foreign['id']:[]}}
    with pytest.raises(PlanInputError): prepare(conn,case,**options)


@pytest.mark.parametrize('bad',['clock_type','recurrence_type','exception_date_type'])
def test_malformed_calendar_candidate_returns_input_error(api_client,domain_connection,addition_case,bad):
    conn,case=domain_connection,addition_case
    user=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    event=add_event(conn,user,day=TODAY)
    options={'calendar_event_overrides':{event['id']:{**event}}}
    if bad=='clock_type': options['calendar_event_overrides'][event['id']]['starts_at']='09:00'
    if bad=='recurrence_type': options['calendar_event_overrides'][event['id']]['recurrence_rule']=123
    if bad=='exception_date_type': options={'calendar_exception_overrides':{event['id']:[{
        'event_id':event['id'],'occurrence_date':[],'kind':'cancelled'}]}}
    with pytest.raises(PlanInputError): prepare(conn,case,**options)
