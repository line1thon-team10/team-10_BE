"""Archived placements disappear without rewriting their historical rows."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.services.plan_draft import _basis
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from tests.e2e.test_monthly_report import world
from tests.test_progress_contract import progress_world


def archive(w, *, applied=True, archived_at=datetime(2026,12,1,2,tzinfo=timezone.utc)):
    if applied:
        domain=PreviewDomain('subject_archive',PlanEffect.UPDATE,_basis,lambda *_:{'ok':True})
        service=ProposalService(w.conn,clock=lambda:archived_at)
        target={'subjectId':w.subject['id']}
        snapshot=service.capture(w.user,domain=domain,target=target)
        proposal=service.create(w.user,domain=domain,target=target,snapshot=snapshot,normalized_input=target,
            changes=target,preview_response={},proposal_type='subject',description='archive fixture')
        service.apply(w.user,proposal['previewId'],domain=domain,target=target)
    w.conn.execute(update(t.subjects).where(t.subjects.c.id==w.subject['id']).values(
        status='archived',archive_reason='user_removed',archived_at=archived_at))


def future_rows(w):
    unit=w.unit(1,10)
    past=w.task((unit,1,10),done=[(unit,1,4)],status='partial',day=w.day-timedelta(days=1))
    future=w.task((unit,1,10),kind='remedial',day=w.day+timedelta(days=1))
    review=w.task(kind='review',day=w.day+timedelta(days=1))
    w.conn.execute(update(t.subjects).where(t.subjects.c.id==w.subject['id']).values(exam_date=w.day+timedelta(days=1)))
    return past,future,review


def test_calendar_preserves_past_and_same_day_before_archive_but_hides_future(api_client,world):
    w=world;past,future,review=future_rows(w); unit=w.unit(1,5)
    early=w.task((unit,1,5));late=w.task((unit,1,5))
    # Archive clock is 11:00 KST; the persisted demo cutoff is November 9.
    for tid,hour in ((early,0),(late,4)):
        w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==tid).values(
            starts_at=w.now.replace(hour=hour),ends_at=w.now.replace(hour=hour+1)))
    archive(w)
    before=w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id.in_([past,future,review,early,late]))).all()
    value=w.get(api_client,'/api/calendar?from=2026-11-08&to=2026-11-15')
    assert {row['id'] for row in value['study']}=={past,early}
    assert not any(row['subjectId']==w.subject['id'] for row in value['exams'])
    assert w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id.in_([past,future,review,early,late]))).all()==before
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=date(2026,11,12)))
    # Advancing the virtual clock must not resurrect the cancelled placement.
    assert {row['id'] for row in w.get(api_client,'/api/calendar?from=2026-11-08&to=2026-11-15')['study']}=={past,early}


@pytest.mark.parametrize('legacy',[True,False])
def test_archive_calendar_preserves_recorded_today(api_client,world,legacy):
    w=world;unit=w.unit(1,10)
    actual=w.task((unit,1,10),done=[(unit,1,5)],status='partial')
    hidden=w.task((unit,1,10),kind='remedial')
    archive(w,applied=not legacy,archived_at=None if legacy else w.now)
    value=w.get(api_client,'/api/calendar?from=2026-11-09&to=2026-11-10')
    assert {row['id'] for row in value['study']}=={actual}
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==hidden))=='planned'


def test_archive_same_day_order_uses_instants_during_dst_fallback(api_client,world):
    from zoneinfo import ZoneInfo
    w=world;unit=w.unit(1,10);day=date(2026,11,1)
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=day,timezone='America/New_York'))
    early=w.task((unit,1,10),day=day);late=w.task((unit,1,10),day=day)
    zone=ZoneInfo('America/New_York')
    for tid,fold in ((early,0),(late,1)):
        start=datetime(2026,11,1,1,45,tzinfo=zone,fold=fold).astimezone(timezone.utc)
        w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==tid).values(starts_at=start,ends_at=start+timedelta(minutes=10)))
    archived=datetime(2026,11,1,1,30,tzinfo=zone,fold=1).astimezone(timezone.utc)
    archive(w,archived_at=archived)
    value=w.get(api_client,'/api/calendar?from=2026-11-01&to=2026-11-01')
    assert {row['id'] for row in value['study']}=={early}
