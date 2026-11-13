from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update

from app.db import schema as t
from tests.test_focus_state import active, action, headers, clock


def test_real_today_clips_midnight_and_excludes_pause(api_client,clock,demo_user,domain_connection):
    # 이미 시드된 42분을 KST 자정 전후로 옮긴다.
    at = datetime(2030,1,2,15,10,tzinfo=timezone.utc)
    clock[0] = at
    conn = domain_connection
    conn.execute(update(t.users).where(t.users.c.id==demo_user).values(demo_date=None))
    conn.execute(update(t.study_sessions).values(started_at=at-timedelta(minutes=42)))
    conn.execute(update(t.study_session_segments).values(started_at=at-timedelta(minutes=42)))
    conn.commit()
    result = active(api_client,demo_user)
    assert result["minutes"]==42 and result["todayMinutes"]==10 and result["subjectMinutes"]==42
    assert action(api_client,demo_user,result["id"],"pause").status_code==200
    clock[0] += timedelta(minutes=20)
    assert action(api_client,demo_user,result["id"],"resume").status_code==200
    clock[0] += timedelta(minutes=1)
    result = active(api_client,demo_user)
    assert result["minutes"]==43 and result["todayMinutes"]==11 and result["pausedMinutes"]==20


@pytest.mark.parametrize("start,end,minutes",[
    (datetime(2030,3,10,6,50,tzinfo=timezone.utc),datetime(2030,3,10,7,10,tzinfo=timezone.utc),20),
    (datetime(2030,11,3,5,50,tzinfo=timezone.utc),datetime(2030,11,3,6,10,tzinfo=timezone.utc),20),
])
def test_dst_uses_elapsed_utc_seconds(api_client,clock,demo_user,domain_connection,start,end,minutes):
    clock[0]=end
    conn=domain_connection
    conn.execute(update(t.users).where(t.users.c.id==demo_user).values(demo_date=None,timezone="America/New_York"))
    conn.execute(update(t.study_sessions).values(started_at=start))
    conn.execute(update(t.study_session_segments).values(started_at=start))
    conn.commit()
    result=active(api_client,demo_user)
    assert result["minutes"]==result["todayMinutes"]==minutes


def test_old_session_view_includes_current_session_but_not_other_subject_total(api_client,clock,demo_user,domain_connection):
    first=active(api_client,demo_user)
    assert action(api_client,demo_user,first["id"],"end").status_code==200
    conn=domain_connection
    other=conn.execute(select(t.study_tasks).where(t.study_tasks.c.subject_id!=first["subjectId"],
        t.study_tasks.c.scheduled_date==datetime(2026,11,9).date())).mappings().first()
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==other["id"]).values(status="planned"))
    conn.commit()
    assert api_client.post("/api/study/sessions",headers=headers(demo_user),json={"taskId":other["id"]}).status_code==200
    clock[0]+=timedelta(minutes=5)
    result=api_client.get(f"/api/study/sessions/{first['id']}",headers=headers(demo_user)).json()["data"]
    assert result["minutes"]==42 and result["todayMinutes"]==47 and result["subjectMinutes"]==42


def test_archived_subject_and_retired_unit_still_describe_owned_history(api_client,clock,demo_user,domain_connection):
    first=active(api_client,demo_user)
    action(api_client,demo_user,first["id"],"end")
    conn=domain_connection
    conn.execute(update(t.subjects).where(t.subjects.c.id==first["subjectId"]).values(status="archived"))
    conn.execute(update(t.units).values(retired_at=clock[0]))
    conn.commit()
    result=api_client.get(f"/api/study/sessions/{first['id']}",headers=headers(demo_user))
    assert result.status_code==200,result.text
    assert result.json()["data"]["subjectName"]=="인공지능" and result.json()["data"]["task"]["ranges"]


def test_empty_ranges_return_null_material(api_client,clock,demo_user,domain_connection):
    first=active(api_client,demo_user)
    domain_connection.execute(delete(t.study_task_ranges).where(t.study_task_ranges.c.task_id==first["taskId"]))
    domain_connection.commit()
    result=active(api_client,demo_user)
    assert result["material"] is None
    assert result["task"]["ranges"]==[] and result["task"]["pages"]==0
    assert result["task"]["from"] is None and result["task"]["to"] is None


def test_ranges_count_union_per_material_and_keep_multiple_pdfs(api_client,clock,demo_user,domain_connection):
    first=active(api_client,demo_user)
    conn=domain_connection
    r=dict(conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==first["taskId"])).mappings().one())
    conn.execute(t.study_task_ranges.insert().values(**{**r,"id":str(uuid4()),"sort_order":2,"start_page":15,"end_page":18}))
    unit=dict(conn.execute(select(t.units).where(t.units.c.id==r["unit_id"])).mappings().one())
    material=dict(conn.execute(select(t.materials).where(t.materials.c.id==unit["material_id"])).mappings().one())
    mid,uid=str(uuid4()),str(uuid4())
    conn.execute(t.materials.insert().values(**{**material,"id":mid,"sha256":"second-material"}))
    conn.execute(t.units.insert().values(**{**unit,"id":uid,"material_id":mid}))
    conn.execute(t.study_task_ranges.insert().values(**{**r,"id":str(uuid4()),"unit_id":uid,"concept_id":None,"sort_order":3}))
    conn.commit()
    result=active(api_client,demo_user)
    assert result["task"]["pages"]==18 and len(result["task"]["ranges"])==3
    assert result["task"]["from"] is None and result["task"]["to"] is None
    assert result["material"]["id"]==material["id"]
