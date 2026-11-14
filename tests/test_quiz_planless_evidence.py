from uuid import uuid4
from sqlalchemy import delete, select
from app.db import schema as t
from app.services.quiz_selection import QuizSelection
from tests.test_progress_contract import progress_world
from tests.test_quiz_selection import prior_seed_quizzes
from tests.test_extra_quiz import base, more


def test_direct_planless_relearning_opens_extra_scope_but_never_daily_progress(api_client,progress_world):
    w=progress_world;unit,parent=base(api_client,w,10)
    record_ids=list(w.conn.scalars(select(t.study_records.c.id).join(t.study_tasks,t.study_tasks.c.id==t.study_records.c.task_id)
        .join(t.study_task_ranges,t.study_task_ranges.c.task_id==t.study_tasks.c.id).where(t.study_task_ranges.c.unit_id==unit['id'])))
    w.conn.execute(delete(t.study_record_ranges).where(t.study_record_ranges.c.record_id.in_(record_ids)))
    block,task,record=(str(uuid4()) for _ in range(3))
    w.conn.execute(t.study_blocks.insert().values(id=block,subject_id=w.subject['id'],unit_id=unit['id'],task_type='review',created_at=w.now))
    w.conn.execute(t.study_tasks.insert().values(id=task,plan_id=None,subject_id=w.subject['id'],block_id=block,
        task_type='review',title='직접 재학습',scheduled_date=w.day,status='completed',estimated_minutes=0,created_at=w.now))
    w.conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=task,unit_id=unit['id'],start_page=1,end_page=10,sort_order=1))
    w.conn.execute(t.study_records.insert().values(id=record,task_id=task,result='completed',study_date=w.day,recorded_at=w.now,updated_at=w.now))
    w.conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=unit['id'],start_page=1,end_page=10))
    user=dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())
    selection=QuizSelection(w.conn,user,w.day)
    assert unit['id'] not in selection.completed(w.subject['id'])
    assert selection.completed(w.subject['id'],today_only=False)[unit['id']][0].end==10
    response=more(api_client,w,unit)
    assert response.status_code==200,response.text
    assert response.json()['data']['count']==5
