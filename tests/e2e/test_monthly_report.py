"""HTTP/database report contracts, including historical and timezone boundaries."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, update

from app.db import schema as t
from tests.test_onboarding_events import events_user
from tests.test_progress_contract import progress_world


@pytest.fixture
def world(progress_world):
    w = progress_world
    w.conn.execute(delete(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==w.user))
    # Keep the real owner/plan but move demo seed evidence out of the test month.
    w.conn.execute(update(t.study_tasks).values(scheduled_date=date(2020, 1, 1)))
    w.conn.execute(update(t.study_records).values(study_date=date(2020, 1, 1)))
    w.conn.execute(update(t.quiz_attempts).values(answered_date=date(2020, 1, 1)))
    w.conn.execute(update(t.mastery_history).values(recorded_date=date(2020, 1, 1)))
    w.conn.execute(update(t.study_sessions).values(study_date=date(2020, 1, 1)))
    return w


def report(client, w, month='2026-11'):
    return w.get(client, '/api/monthly/report?month='+month)


def quiz(w, unit, day, *, correct=True, confidence='sure', finalized=True, owner=None, score=None):
    qid, item, session, attempt = [str(uuid4()) for _ in range(4)]
    at = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=1)
    w.conn.execute(t.questions.insert().values(id=qid, unit_id=unit['id'], type='short', difficulty='low',
        prompt='질문', correct_answer='답', origin='pregenerated', created_at=at))
    w.conn.execute(t.quiz_sessions.insert().values(id=session, user_id=owner or w.user, subject_id=w.subject['id'],
        type='daily', quiz_date=day, status='submitted', grading_status='completed' if finalized else 'pending',
        created_at=at, submitted_at=at, finalized_at=at if finalized else None, graded_at=at if finalized else None))
    w.conn.execute(t.quiz_items.insert().values(id=item, quiz_session_id=session, question_id=qid,
        sort_order=0, inclusion_reason='daily'))
    w.conn.execute(t.quiz_attempts.insert().values(id=attempt, quiz_item_id=item, answer='답', confidence=confidence,
        is_correct=correct, grading_status='graded', answered_at=at, answered_date=day,
        finalized_at=at if finalized else None, graded_at=at if finalized else None))
    if score is not None:
        w.conn.execute(t.mastery_history.insert().values(id=str(uuid4()), unit_id=unit['id'], quiz_session_id=session,
            raw_score=score, score=score, level='stable' if score>=80 else 'shaky' if score>=50 else 'risk',
            recovery_mode=False, recorded_date=day, created_at=at))
    return session, attempt


def focus(w, task, day, spans=(), *, measured=None, owner=None):
    sid = str(uuid4())
    spans = [(start.astimezone(timezone.utc), end.astimezone(timezone.utc)) for start, end in spans]
    at = spans[0][0] if spans else w.now
    w.conn.execute(t.study_sessions.insert().values(id=sid, user_id=owner or w.user, task_id=task,
        status='ended', started_at=at, ended_at=spans[-1][1] if spans else at,
        measured_seconds=measured, study_date=day, created_at=at))
    for order, (start, end) in enumerate(spans):
        w.conn.execute(t.study_session_segments.insert().values(id=str(uuid4()), session_id=sid,
            sort_order=order, started_at=start, ended_at=end))
    return sid


def test_future_and_empty_month_are_empty_even_with_future_facts(api_client, world):
    w=world; unit=w.unit(1,10)
    w.task((unit,1,10),done=[(unit,1,10)],status='completed',day=date(2026,12,1))
    quiz(w,unit,date(2026,12,1),score=90)
    future=report(api_client,w,'2026-12')
    assert future['from']=='2026-12-01' and future['until']=='2026-11-09'
    for value in (future,report(api_client,w,'2019-01')):
        assert value['studyDays']==value['plannedDays']==0
        assert value['achievement']=={'done':0,'plan':0,'rate':0}
        assert value['time']=={'measuredMinutes':0,'plannedMinutes':0}
        assert value['quiz']=={'total':0,'wrong':0,'unsureRight':0}
        assert value['grades']==value['subjects']==[]


def test_achievement_counts_placements_while_share_deduplicates_material_pages(api_client,world):
    w=world;a=w.unit(1,10);b=w.unit(1,10)
    w.task((a,1,10),done=[(a,1,10)],status='completed',day=date(2026,11,1))
    w.task((a,1,10),done=[(a,1,10)],status='completed',day=date(2026,11,2))
    w.task((a,1,10),done=[(a,1,10)],status='completed',kind='remedial',day=date(2026,11,3))
    w.task((b,1,10),done=[(b,1,5)],status='partial',day=date(2026,11,4))
    w.task((a,1,10),done=[(a,1,10)],status='completed',day=date(2026,11,10))
    value=report(api_client,w)
    assert value['achievement']=={'plan':40,'done':35,'rate':88}
    assert value['subjects']==[{'subjectId':w.subject['id'],'pages':15,'share':100.0}]
    assert value['studyDays']==value['plannedDays']==4
    assert value['time']=={'measuredMinutes':0,'plannedMinutes':100}
    # Reads do not introduce cached facts or rewrite totals.
    assert report(api_client,w)==value


def test_closed_focus_segments_clip_local_month_and_exclude_pauses(api_client,world,monkeypatch):
    w=world; unit=w.unit();task=w.task((unit,1,10),day=date(2026,11,1))
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=None,timezone='Asia/Seoul'))
    monkeypatch.setattr('app.services.monthly.today_for',lambda user: date(2026,11,9))
    monkeypatch.setattr('app.services.monthly_report.now_utc',lambda:datetime(2026,11,9,12,tzinfo=timezone.utc))
    zone=ZoneInfo('Asia/Seoul')
    at=lambda day,h,m:datetime(2026,10 if day==31 else 11,day,h,m,tzinfo=zone)
    focus(w,task,date(2026,10,31),[(at(31,23,45),at(1,0,15)),(at(1,0,30),at(1,0,40))],measured=99999)
    # End exactly at month boundary contributes nothing to November.
    focus(w,task,date(2026,10,31),[(at(31,23,30),at(1,0,0))],measured=1800)
    value=report(api_client,w)
    assert value['time']['measuredMinutes']==25 and value['studyDays']==1
    assert report(api_client,w,'2026-10')['time']['measuredMinutes']==45


def test_demo_focus_uses_study_date_not_real_clock_and_preserves_partial_minutes(api_client,world):
    w=world;unit=w.unit();task=w.task((unit,1,10))
    instant=datetime(2025,2,1,tzinfo=timezone.utc)
    focus(w,task,date(2026,11,1),[(instant,instant+timedelta(seconds=35))],measured=9999)
    focus(w,task,date(2026,11,1),[(instant+timedelta(minutes=1),instant+timedelta(minutes=1,seconds=35))])
    focus(w,task,date(2026,10,31),[(instant,instant+timedelta(hours=10))])
    focus(w,task,date(2026,11,10),[(instant,instant+timedelta(hours=10))])
    focus(w,task,date(2026,11,2),measured=90)
    value=report(api_client,w)
    assert value['time']['measuredMinutes']==2 and value['studyDays']==2


def test_quiz_requires_finalized_owned_attempts_and_counts_unsure_right_separately(api_client,world,events_user):
    w=world;unit=w.unit()
    quiz(w,unit,date(2026,11,1),correct=False)
    quiz(w,unit,date(2026,11,2),confidence='unsure')
    quiz(w,unit,date(2026,11,2))
    quiz(w,unit,date(2026,11,3),finalized=False)
    quiz(w,unit,date(2026,11,10))
    quiz(w,unit,date(2026,10,31))
    quiz(w,unit,date(2026,11,4),owner=events_user['id'])
    _,attempt=quiz(w,unit,date(2026,11,5))
    w.conn.execute(update(t.quiz_attempts).where(t.quiz_attempts.c.id==attempt).values(graded_at=None))
    value=report(api_client,w)
    assert value['quiz']=={'total':3,'wrong':1,'unsureRight':1}
    assert value['studyDays']==2
    assert api_client.get('/api/monthly/report?month=2026-11',headers={'X-Anonymous-Id':events_user['id']}).json()['data']['quiz']['total']==0


def test_grades_use_historical_boundaries_and_distinct_pdf_identity(api_client,world):
    w=world; a=w.unit(code='U1');b=w.unit(code='U1'); unseen=w.unit(code='U2',score=90)
    quiz(w,a,date(2026,10,31),score=55)
    quiz(w,a,date(2026,11,2),score=70)
    quiz(w,a,date(2026,11,8),score=81)
    quiz(w,a,date(2026,11,10),score=99)
    quiz(w,b,date(2026,11,3),score=0)
    quiz(w,b,date(2026,11,8),score=90,finalized=False)
    w.conn.execute(update(t.units).where(t.units.c.id==a['id']).values(mastery_score=99,retired_at=w.now))
    value=report(api_client,w)
    grades={row['unitId']:row for row in value['grades']}
    assert grades[a['id']]['before']=={'grade':'shaky','score':55}
    assert grades[a['id']]['after']=={'grade':'stable','score':81}
    assert grades[b['id']]['before'] is None
    assert grades[b['id']]['after']=={'grade':'risk','score':0}
    assert grades[a['id']]['unitNo']==grades[b['id']]['unitNo']==1
    assert grades[a['id']]['materialId']!=grades[b['id']]['materialId']
    assert unseen['id'] not in grades
    october=report(api_client,w,'2026-10')
    assert next(row for row in october['grades'] if row['unitId']==a['id'])['after']['score']==55


@pytest.mark.parametrize('month',['2026-00','2026-13','2026-1','2026-11-01','0000-01'])
def test_report_invalid_month_is_422(api_client,events_user,month):
    assert api_client.get('/api/monthly/report',params={'month':month},headers={'X-Anonymous-Id':events_user['id']}).status_code==422


def test_report_requires_owner_authentication(api_client):
    assert api_client.get('/api/monthly/report?month=2026-11').status_code==401


def test_subject_share_excludes_supplement_and_keeps_archived_learning(api_client,world):
    w=world; original=w.subject; a=w.unit(1,10)
    w.task((a,1,10),done=[(a,1,4)],status='partial')
    w.task((a,1,10),done=[(a,1,10)],kind='remedial',status='completed')
    other={**dict(original),'id':str(uuid4()),'code':'REPORT','name':'두번째 과목','status':'archived'}
    w.conn.execute(t.subjects.insert().values(**other))
    w.subject=other
    b=w.unit(1,10)
    w.task((b,1,10),done=[(b,1,6)],status='completed')
    # A second historical placement does not count the same PDF pages twice.
    w.task((b,1,6),done=[(b,1,6)],status='excluded')
    value=report(api_client,w)
    assert {s['subjectId']:(s['pages'],s['share']) for s in value['subjects']}=={
        original['id']:(4,40.0),other['id']:(6,60.0)}
    assert value['achievement']=={'plan':30,'done':20,'rate':67}


def test_focus_counts_dst_elapsed_time_and_rejects_foreign_owner(api_client,world,events_user,monkeypatch):
    w=world;unit=w.unit();task=w.task((unit,1,10))
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=None,timezone='America/New_York'))
    monkeypatch.setattr('app.services.monthly.today_for',lambda user:date(2026,11,9))
    monkeypatch.setattr('app.services.monthly_report.now_utc',lambda:datetime(2026,11,9,12,tzinfo=timezone.utc))
    zone=ZoneInfo('America/New_York')
    span=(datetime(2026,11,1,0,30,tzinfo=zone),datetime(2026,11,1,2,30,tzinfo=zone))
    focus(w,task,date(2026,11,1),[span])
    focus(w,task,date(2026,11,1),[span],owner=events_user['id'])
    value=report(api_client,w)
    assert value['time']['measuredMinutes']==180
    assert value['studyDays']==1


def test_unpaged_completed_review_is_study_day_with_zero_page_rate(api_client,world):
    w=world
    w.task(done=[],status='completed',kind='review',day=date(2026,11,2))
    value=report(api_client,w)
    assert value['studyDays']==value['plannedDays']==1
    assert value['achievement']=={'done':0,'plan':0,'rate':0}
    assert value['time']['plannedMinutes']==25
    assert value['subjects']==[]


def test_closing_a_day_does_not_restore_excluded_planned_minutes(api_client,world):
    from app.services.daily_totals import DailyTotalsService
    w=world;unit=w.unit(1,10)
    w.task((unit,1,10),done=[(unit,1,10)],status='completed')
    w.task((unit,1,10),status='excluded')
    before=report(api_client,w)
    user=dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())
    DailyTotalsService(w.conn,user).snapshot(w.day,finalize=True,at=w.now)
    after=report(api_client,w)
    assert before['time']==after['time']=={'measuredMinutes':0,'plannedMinutes':25}
    assert before['achievement']==after['achievement']=={'plan':10,'done':10,'rate':100}
