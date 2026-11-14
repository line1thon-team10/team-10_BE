from datetime import timedelta
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from app.domain.user import User
from app.repositories.users import UserRepository
from tests.test_onboarding_events import events_user, add_event, add_exception, add_subject, MONDAY, NOW
from tests.test_progress_contract import progress_world


def get(client, user, first='2026-11-09', last='2026-11-15'):
    return client.get('/api/calendar', headers={'X-Anonymous-Id': user}, params={'from':first,'to':last})


@pytest.mark.parametrize('first,last', [('2026-11-15','2026-11-09'),('2026-11-01','2026-12-13'),
    ('2026-02-29','2026-03-01'),('2026-1-01','2026-01-02'),('x','2026-01-01')])
def test_calendar_rejects_invalid_or_unbounded_range(api_client, events_user, first, last):
    assert get(api_client,events_user['id'],first,last).status_code == 422


def test_empty_account_and_inclusive_42_day_limit(api_client, events_user):
    response=get(api_client,events_user['id'],'2026-11-01','2026-12-12')
    assert response.status_code == 200, response.text
    data=response.json()['data']
    assert data['fixed']==data['study']==data['exams']==[] and data['summary']=={}
    assert data['today']=='2026-11-11'
    assert api_client.get('/api/calendar',params={'from':'2026-11-01','to':'2026-11-01'}).status_code==401


def test_original_occurrence_id_survives_moves_and_cancelled_days_disappear(api_client, domain_connection, events_user):
    conn,user=domain_connection,events_user
    row=add_event(conn,user,day=MONDAY-timedelta(days=7),recurrence_rule='FREQ=WEEKLY;BYDAY=MO,WE',recurrence_until=MONDAY+timedelta(days=2))
    add_exception(conn,row,MONDAY,kind='cancelled')
    original=MONDAY-timedelta(days=7)
    add_exception(conn,row,original,kind='moved',moved_day=MONDAY+timedelta(days=1))
    before=(conn.execute(select(t.calendar_events)).all(),conn.execute(select(t.calendar_event_exceptions)).all())
    conn.commit()
    response=get(api_client,user['id'])
    assert response.status_code==200,response.text
    rows=response.json()['data']['fixed']
    assert [(r['id'],r['date']) for r in rows]==[(f"{row['id']}@{original}",'2026-11-10'),(f"{row['id']}@2026-11-11",'2026-11-11')]
    assert all(r['revision']==1 and r['repeat'] for r in rows)
    assert (conn.execute(select(t.calendar_events)).all(),conn.execute(select(t.calendar_event_exceptions)).all())==before


def test_calendar_filters_foreign_deleted_and_foreign_subject_events(api_client, domain_connection, events_user):
    conn,user=domain_connection,events_user
    other=User.anonymous(now=NOW);UserRepository(conn).add(other)
    sid=add_subject(conn,other.id,exam_date=MONDAY)
    add_event(conn,other.to_mapping())
    add_event(conn,user,subject_id=sid)
    add_event(conn,user,deleted_at=NOW)
    own=add_subject(conn,user['id'],exam_date=MONDAY)
    conn.commit()
    data=get(api_client,user['id']).json()['data']
    assert data['fixed']==[]
    assert [e['subjectId'] for e in data['exams']]==[own]
    assert data['exams'][0]['color']=='#123456'


def test_composite_material_ranges_and_supplement_summary(api_client, progress_world):
    w=progress_world
    a,b=w.unit(1,5),w.unit(1,5)
    task=w.task((a,1,5),(b,1,5),done=[(a,1,3)],status='partial')
    supplement=w.task((a,1,2),kind='remedial')
    w.conn.commit()
    response=get(api_client,w.user)
    assert response.status_code==200,response.text
    data=response.json()['data'];rows={x['id']:x for x in data['study']}
    assert set(rows)=={task,supplement}
    assert rows[task]['pages']==10 and rows[task]['from'] is None and rows[task]['to'] is None
    assert len(rows[task]['ranges'])==2 and rows[supplement]['supplement']
    assert data['summary']=={w.subject['id']:{'minutes':50,'count':2}}


def test_local_day_boundary_and_multiday_clipping(api_client, domain_connection, events_user):
    conn,user=domain_connection,events_user
    row=add_event(conn,user,day=MONDAY-timedelta(days=1),start='23:30',end='23:59')
    from tests.test_onboarding_events import at
    conn.execute(update(t.calendar_events).where(t.calendar_events.c.id==row['id']).values(ends_at=at(MONDAY,'01:00')))
    conn.commit()
    response=get(api_client,user['id'],'2026-11-09','2026-11-09')
    assert response.status_code==200,response.text
    fixed=response.json()['data']['fixed']
    assert len(fixed)==1
    assert fixed[0]['id'].endswith('@2026-11-08')
    assert (fixed[0]['date'],fixed[0]['start'],fixed[0]['end'])==('2026-11-09','00:00','01:00')


def test_year_one_boundary_is_valid_empty_range(api_client, events_user):
    response=get(api_client,events_user['id'],'0001-01-01','0001-01-01')
    assert response.status_code==200,response.text
    assert response.json()['data']['fixed']==[]
