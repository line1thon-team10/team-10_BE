from datetime import date,datetime,timezone
from uuid import uuid4
from sqlalchemy import select,update
from app.db import schema as t
from app.services.subject_archive_state import archived_date,archived_dates
from app.services.plan_draft import _basis
from app.services.proposals import PlanEffect,PreviewDomain,ProposalService
from tests.test_onboarding_events import events_user,add_subject,NOW


def test_cutoff_preserves_demo_approval_date_after_advancing_clock(domain_connection,events_user):
    conn,user=domain_connection,events_user
    sid=add_subject(conn,user['id'],status='archived')
    subject=dict(conn.execute(select(t.subjects).where(t.subjects.c.id==sid)).mappings().one())
    domain=PreviewDomain('subject_archive',PlanEffect.DRAFT,_basis,lambda *_:{'ok':True})
    service=ProposalService(conn,clock=lambda:NOW)
    target={'subjectId':sid}
    snapshot=service.capture(user['id'],domain=domain,target=target)
    candidate=service.create(user['id'],domain=domain,target=target,snapshot=snapshot,normalized_input=target,
        changes=target,preview_response={},proposal_type='subject',description='archive fixture')
    service.apply(user['id'],candidate['previewId'],domain=domain,target=target)
    conn.commit()
    user={**user,'demo_date':date(2026,12,20)}
    assert archived_date(conn,user,subject)==date(2026,11,11)
    foreign={**user,'id':str(uuid4())}
    assert archived_date(conn,foreign,subject)==date(2026,12,20)


def test_cutoff_distinguishes_exam_day_and_actual_local_archive_time(domain_connection,events_user):
    conn,user=domain_connection,events_user
    ids=[add_subject(conn,user['id'],status='archived') for _ in range(3)]
    conn.execute(update(t.subjects).where(t.subjects.c.id==ids[0]).values(archive_reason='exam_finished',
        exam_date=date(2026,11,9),archived_at=NOW))
    conn.execute(update(t.subjects).where(t.subjects.c.id==ids[1]).values(archive_reason='user_removed',
        archived_at=datetime(2026,11,8,23,30,tzinfo=timezone.utc)))
    subjects=[dict(row) for row in conn.execute(select(t.subjects).where(t.subjects.c.id.in_(ids))).mappings()]
    assert archived_dates(conn,user,subjects)=={ids[0]:date(2026,11,10),ids[1]:date(2026,11,9),ids[2]:date(2026,11,11)}
    assert archived_date(conn,user,{**subjects[0],'status':'active'}) is None
