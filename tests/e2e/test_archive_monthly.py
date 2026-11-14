from datetime import date, timedelta

from sqlalchemy import event, select, update

from app.db import schema as t
from app.db import session
from tests.e2e.test_archive_calendar import archive, future_rows, world
from tests.test_onboarding_events import events_user
from tests.test_progress_contract import progress_world


def test_monthly_keeps_actual_history_but_hides_archived_review_and_exam(api_client,world,events_user):
    w=world;past,future,review=future_rows(w)
    archive(w)
    data=w.get(api_client,'/api/monthly?month=2026-11')
    days={row['date']:row for row in data['days']}
    old=days[(w.day-timedelta(days=1)).isoformat()]
    assert (old['plan'],old['done'])==(10,4)
    assert old['bySubject']==[{'subjectId':w.subject['id'],'pages':4}]
    future_day=days[(w.day+timedelta(days=1)).isoformat()]
    assert not future_day['planned'] and future_day['labels']==[] and not future_day['estimated']
    assert (future_day['plan'],future_day['done'],future_day['rate'])==(0,0,0)
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=date(2026,11,12)))
    report=w.get(api_client,'/api/monthly/report?month=2026-11')
    assert report['achievement']=={'done':4,'plan':10,'rate':40}
    assert report['time']['plannedMinutes']==25
    assert report['subjects']==[{'subjectId':w.subject['id'],'pages':4,'share':100.0}]
    assert report['studyDays']==report['plannedDays']==1
    other=api_client.get('/api/monthly/report?month=2026-11',headers={'X-Anonymous-Id':events_user['id']}).json()['data']
    assert other['subjects']==other['grades']==[] and other['achievement']['plan']==0
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==future))=='planned'


def test_monthly_reads_archive_cutoff_and_study_history_once_per_request(api_client,world):
    w=world;future_rows(w);archive(w);w.conn.commit()
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        statements.append(' '.join(statement.split()))
    event.listen(session.engine,'before_cursor_execute',capture)
    try:
        response=api_client.get('/api/monthly?month=2026-11',headers={'X-Anonymous-Id':w.user})
        assert response.status_code==200,response.text
        assert len(response.json()['data']['days'])==42
    finally:
        event.remove(session.engine,'before_cursor_execute',capture)
    assert sum('FROM plan_proposal_states' in sql for sql in statements)==1
    assert sum('FROM study_tasks JOIN subjects' in sql for sql in statements)==1
    assert not any(sql.startswith(('INSERT ','UPDATE ','DELETE ')) for sql in statements)
