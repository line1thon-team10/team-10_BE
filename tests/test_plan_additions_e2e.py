from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case


@pytest.fixture
def range_case(domain_connection, addition_case):
    domain_connection.execute(update(t.materials).where(t.materials.c.id==addition_case['material']).values(page_count=40))
    domain_connection.commit()
    return addition_case


def body(case, **changes):
    return {'subjectId':case['subject'],'materialId':case['material'],'from':21,'to':26,
        'minutesPerDay':30,'days':[2,3], 'startDate':(TODAY+timedelta(days=1)).isoformat(),
        'endDate':(TODAY+timedelta(days=2)).isoformat(),**changes}


def preview(client,case,**changes):
    return client.post('/api/plan/additions/preview',headers={'X-Anonymous-Id':case['user']},json=body(case,**changes))


def apply(client,case,key):
    return client.post('/api/plan/additions',headers={'X-Anonymous-Id':case['user']},json={'previewId':key})


def facts(conn):
    return {table.name:conn.execute(select(table).order_by(*table.primary_key.columns)).all()
        for table in (t.units,t.concepts,t.study_tasks,t.study_task_ranges,t.study_records,t.study_record_ranges,t.plans)}


def test_preview_then_apply_adds_exact_pages_and_replays(api_client,domain_connection,range_case):
    case,conn=range_case,domain_connection
    before=facts(conn);conn.commit()
    response=preview(api_client,case)
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert value['conflicts']==value['leftPages']==0
    assert value['summary']['totalPages']==6 and value['summary']['goalType']=='time'
    assert sorted(p for row in value['placements'] for p in range(row['from'],row['to']+1))==list(range(21,27))
    assert all(row['date'] in ['2026-11-17','2026-11-18'] for row in value['placements'])
    assert facts(conn)==before;conn.commit()
    assert preview(api_client,case).json()==response.json()
    applied=apply(api_client,case,value['previewId'])
    assert applied.status_code==200 and applied.json()['data']=={'applied':True},applied.text
    rows=conn.execute(select(t.study_task_ranges).join(t.study_tasks,t.study_tasks.c.id==t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.subject_id==case['subject'],t.study_tasks.c.status=='planned')).mappings().all()
    assert sorted(p for row in rows for p in range(row['start_page'],row['end_page']+1))==list(range(1,27))
    assert conn.scalar(select(t.plans.c.revision))==2
    after=facts(conn);conn.commit()
    assert apply(api_client,case,value['previewId']).json()==applied.json()
    assert facts(conn)==after


def test_page_goal_uses_only_prefix_and_can_combine_short_tail(api_client,domain_connection,range_case):
    case=range_case
    response=preview(api_client,case,minutesPerDay=None,totalPages=2)
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['summary']['goalType']=='pages' and data['summary']['minutesPerDay'] is None
    assert sorted(p for row in data['placements'] for p in range(row['from'],row['to']+1))==[21,22]
    assert apply(api_client,case,data['previewId']).status_code==200
    tasks=domain_connection.execute(select(t.study_tasks).where(t.study_tasks.c.status=='planned')).mappings().all()
    assert all(15<=row['estimated_minutes']<=60 for row in tasks)


def test_daily_limit_counts_selected_pages_only_even_in_mixed_task(api_client,range_case):
    response=preview(api_client,range_case,minutesPerDay=9)
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert value['conflicts']==0
    totals={day:sum(row['minutes'] for row in value['placements'] if row['date']==day) for day in ['2026-11-17','2026-11-18']}
    assert totals=={'2026-11-17':9,'2026-11-18':9}


def test_infeasible_window_is_reviewable_but_cannot_apply(api_client,domain_connection,range_case):
    case,conn=range_case,domain_connection
    before=facts(conn);conn.commit()
    response=preview(api_client,case,days=[7])
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert value['conflicts']>0 and value['leftPages']==6 and value['placements']==[]
    applied=apply(api_client,case,value['previewId'])
    assert applied.status_code==409 and applied.json()['error']['code']=='PLAN_INFEASIBLE'
    assert facts(conn)==before


@pytest.mark.parametrize('change',['foreign_subject','foreign_material','expired_date','exam_day','outside_pdf'])
def test_owned_physical_scope_and_date_boundaries(api_client,range_case,change):
    case=range_case
    values={'foreign_subject':{'subjectId':case['foreign']},'foreign_material':{'materialId':case['added_material']},
        'expired_date':{'startDate':(TODAY-timedelta(days=1)).isoformat()},
        'exam_day':{'endDate':(TODAY+timedelta(days=3)).isoformat()},'outside_pdf':{'to':41}}[change]
    response=preview(api_client,case,**values)
    assert response.status_code in (404,422),response.text


def test_new_fact_makes_candidate_stale_and_other_user_cannot_apply(api_client,domain_connection,range_case):
    case,conn=range_case,domain_connection
    response=preview(api_client,case);assert response.status_code==200,response.text
    key=response.json()['data']['previewId']
    foreign=apply(api_client,{**case,'user':case['other_user']},key)
    assert foreign.status_code==404
    conn.execute(t.study_records.insert().values(id=str(uuid4()),task_id=case['old_task']['id'],result='none',
        study_date=TODAY,recorded_at=NOW,updated_at=NOW));conn.commit()
    response=apply(api_client,case,key)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE'


def test_two_candidates_cannot_both_replace_same_revision(api_client,range_case):
    a=preview(api_client,range_case).json()['data']['previewId']
    b=preview(api_client,range_case,to=28).json()['data']['previewId']
    assert a!=b and apply(api_client,range_case,a).status_code==200
    response=apply(api_client,range_case,b)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE'


def test_structure_and_tasks_rollback_together_on_apply_failure(api_client,domain_connection,range_case,monkeypatch):
    from app.services import plan_additions
    case,conn=range_case,domain_connection
    before=facts(conn);conn.commit()
    key=preview(api_client,case).json()['data']['previewId']
    original=plan_additions.apply_placements
    def fail(*args):
        original(*args)
        raise RuntimeError('injected')
    monkeypatch.setattr(plan_additions,'apply_placements',fail)
    assert apply(api_client,case,key).status_code==500
    assert facts(conn)==before;conn.commit()
    monkeypatch.setattr(plan_additions,'apply_placements',original)
    assert apply(api_client,case,key).status_code==200


def test_partial_excluded_concept_addition_does_not_include_its_neighbors(api_client,domain_connection,range_case):
    case,conn=range_case,domain_connection
    identifiers=[]
    for index,(start,end,included) in enumerate([(1,10,True),(11,20,False)],1):
        identifier=str(uuid4());identifiers.append(identifier)
        conn.execute(t.concepts.insert().values(id=identifier,unit_id=case['unit'],code=str(index),title='원래 개념',
            sort_order=index,start_page=start,end_page=end,in_scope=included,created_at=NOW,updated_at=NOW))
    conn.execute(update(t.units).where(t.units.c.id==case['unit']).values(mastery_score=72,mastery_level='shaky'))
    conn.commit()
    response=preview(api_client,case,**{'from':15,'to':17})
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId']).status_code==200
    old=conn.execute(select(t.units).where(t.units.c.id==case['unit'])).mappings().one()
    assert old['retired_at'] is not None and old['mastery_score']==72
    rows=conn.execute(select(t.study_task_ranges).join(t.study_tasks,t.study_tasks.c.id==t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.subject_id==case['subject'],t.study_tasks.c.status=='planned')).mappings().all()
    assert sorted(p for row in rows for p in range(row['start_page'],row['end_page']+1))==[*range(1,11),15,16,17]
    assert all(conn.scalar(select(t.concepts.c.retired_at).where(t.concepts.c.id==identifier)) is not None for identifier in identifiers)


def test_other_active_subject_tasks_are_byte_for_byte_unchanged(api_client,domain_connection,range_case):
    from tests.test_plan_draft_e2e import draft, approve as initial_approve
    case,conn=range_case,domain_connection
    response=draft(api_client,case,draftSubjectIds=case['added'])
    assert response.status_code==200,response.text
    assert initial_approve(api_client,case,response.json()['data']['previewId']).status_code==200
    tasks=conn.execute(select(t.study_tasks).where(t.study_tasks.c.subject_id==case['added'])).all()
    ids=[row.id for row in tasks]
    ranges=conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_(ids))).all()
    conn.commit()
    response=preview(api_client,case)
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId']).status_code==200
    assert conn.execute(select(t.study_tasks).where(t.study_tasks.c.subject_id==case['added'])).all()==tasks
    assert conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_(ids))).all()==ranges


@pytest.mark.parametrize('existing',['learned','running','ended_unconsumed'])
def test_selected_learning_or_started_facts_are_not_duplicated(api_client,domain_connection,range_case,existing):
    case,conn=range_case,domain_connection
    task=case['old_task']
    if existing=='learned':
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(
            scheduled_date=TODAY-timedelta(days=1),starts_at=None,ends_at=None,status='partial'))
        identifier=str(uuid4())
        conn.execute(t.study_records.insert().values(id=identifier,task_id=task['id'],result='partial',
            study_date=TODAY-timedelta(days=1),recorded_at=NOW,updated_at=NOW))
        conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=identifier,unit_id=case['unit'],start_page=1,end_page=5))
    else:
        conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=case['user'],task_id=task['id'],
            status='running' if existing=='running' else 'ended',started_at=NOW,
            ended_at=None if existing=='running' else NOW+timedelta(minutes=10),
            measured_seconds=0 if existing=='running' else 600,study_date=TODAY,created_at=NOW))
    before=facts(conn);conn.commit()
    response=preview(api_client,case,**{'from':1,'to':6})
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert value['summary']['totalPages']==6 and value['leftPages']==value['conflicts']==0
    selected_pages=sorted(p for row in value['placements'] for p in range(row['from'],row['to']+1))
    assert selected_pages==([6] if existing=='learned' else [])
    assert facts(conn)==before;conn.commit()
    assert apply(api_client,case,value['previewId']).status_code==200
    if existing!='learned':
        assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==task['id'])).mappings().one())==task
    assert conn.execute(select(t.study_records).order_by(*t.study_records.primary_key.columns)).all()==before['study_records']
    assert conn.execute(select(t.study_record_ranges).order_by(*t.study_record_ranges.primary_key.columns)).all()==before['study_record_ranges']


def test_no_plan_has_explicit_no_plan_error(api_client,plan_case):
    response=preview(api_client,plan_case)
    assert response.status_code==400 and response.json()['error']['code']=='NO_PLAN'


@pytest.mark.parametrize('multiple_gaps',[False,True])
def test_new_manual_unit_number_stays_within_database_integer_bounds(api_client,domain_connection,range_case,multiple_gaps):
    case=range_case
    domain_connection.execute(update(t.units).where(t.units.c.id==case['unit']).values(
        code='unit_2147483647',sort_order=2147483647))
    if multiple_gaps:
        row=dict(domain_connection.execute(select(t.units).where(t.units.c.id==case['unit'])).mappings().one())
        domain_connection.execute(t.units.insert().values(**{**row,'id':str(uuid4()),'code':'unit_2','sort_order':2,'start_page':24,'end_page':25}))
    domain_connection.commit()
    response=preview(api_client,case,to=28 if multiple_gaps else 26)
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId']).status_code==200
    rows=domain_connection.execute(select(t.units.c.sort_order).where(t.units.c.material_id==case['material'],t.units.c.retired_at.is_(None))).scalars().all()
    assert len(rows)==len(set(rows))==(4 if multiple_gaps else 2) and all(1<=number<=2147483647 for number in rows)
