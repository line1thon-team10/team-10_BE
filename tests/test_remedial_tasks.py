"""Real submit -> weak concepts -> durable obligation -> legal task placement."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import func, select, update
from app.ai.metrics_adapter import current_scope
from app.db import schema as t
from app.services.quiz import QuizService
from app.services.remedial import generate_remedial, schedule_obligation
from tests.test_mastery_rules import mastery_case, make_quiz, DAY, AT
from tests.test_onboarding_events import at


@pytest.fixture
def remedial_case(domain_connection,mastery_case):
    conn,c=domain_connection,mastery_case;uid=c['units'][0]['id'];mid=c['units'][0]['material_id']
    conn.execute(update(t.subjects).where(t.subjects.c.id==c['subject_id']).values(exam_date=DAY+timedelta(days=4),target_score=90,minutes_per_page=5))
    concept=conn.scalar(select(t.concepts.c.id).where(t.concepts.c.unit_id==uid))
    conn.execute(update(t.concepts).where(t.concepts.c.id==concept).values(end_page=3))
    other,last=str(uuid4()),str(uuid4())
    for index,start,end,cid in [(2,4,6,other),(3,8,10,last)]:
        conn.execute(t.concepts.insert().values(id=cid,unit_id=uid,code=f'C{index}',title=f'개념{index}',sort_order=index,start_page=start,end_page=end,created_at=AT,updated_at=AT))
    for index,qid in enumerate(c['questions'][0]):
        conn.execute(update(t.questions).where(t.questions.c.id==qid).values(concept_id=concept if index<7 else other if index<14 else last))
    plan,block,task,record=[str(uuid4()) for _ in range(4)]
    conn.execute(t.plans.insert().values(id=plan,user_id=c['user'].id,revision=1,created_at=AT,approved_at=AT))
    conn.execute(t.study_blocks.insert().values(id=block,subject_id=c['subject_id'],unit_id=uid,task_type='study',created_at=AT))
    conn.execute(t.study_tasks.insert().values(id=task,plan_id=plan,subject_id=c['subject_id'],block_id=block,task_type='study',title='첫 학습',scheduled_date=DAY,estimated_minutes=45,status='completed',created_at=AT))
    conn.execute(t.study_records.insert().values(id=record,task_id=task,result='completed',recorded_at=AT,study_date=DAY,updated_at=AT))
    for order,(a,b) in enumerate([(1,6),(8,10)]):
        conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=task,unit_id=uid,start_page=a,end_page=b,sort_order=order))
        conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=uid,start_page=a,end_page=b))
    metrics=QuizService(conn,c['user'].to_mapping(),DAY)._metrics_input();unit=next(x for x in metrics['units'] if x['id']==uid)
    pass_id=str(uuid4())
    conn.execute(t.learning_passes.insert().values(id=pass_id,unit_id=uid,pass_number=1,completion_kind='initial',source_task_id=task,evidence_key='initial',
        evidence_snapshot={'schemaVersion':'1','materialId':mid,'recordIds':[record],'requiredRanges':[[1,6],[8,10]],'masteryScopeRevision':current_scope(unit,metrics['concepts']).revision},completed_at=AT,completed_date=DAY))
    block2,task2=str(uuid4()),str(uuid4());nextday=DAY+timedelta(days=2)
    conn.execute(t.study_blocks.insert().values(id=block2,subject_id=c['subject_id'],unit_id=uid,task_type='study',created_at=AT))
    conn.execute(t.study_tasks.insert().values(id=task2,plan_id=plan,subject_id=c['subject_id'],block_id=block2,task_type='study',title='기존 미래 학습',scheduled_date=nextday,
        starts_at=at(nextday,'09:00'),ends_at=at(nextday,'09:30'),estimated_minutes=30,status='planned',created_at=AT))
    conn.commit();return {**c,'plan':plan,'initial':pass_id,'record':record,'existing':task2,'nextday':nextday}


def submit(client,conn,c):
    quiz,items=make_quiz(conn,c,c['questions'][0],day=DAY)
    body={'quizId':quiz,'answers':[{'questionId':item['id'],'choice':0 if 7<=i<14 else 1,'confidence':'sure'} for i,item in enumerate(items)]}
    response=client.post(f'/api/quiz/{quiz}/submit',headers=c['headers'],json=body)
    assert response.status_code==200,response.text
    return quiz,body,response.json()['data']


def test_discontinuous_wrong_concepts_use_next_subject_study_day_and_replay_once(api_client,domain_connection,remedial_case):
    c,conn=remedial_case,domain_connection
    before=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==c['existing'])).mappings().one());conn.commit()
    quiz,body,result=submit(api_client,conn,c)
    obligation=conn.execute(select(t.remedial_obligations)).mappings().one()
    assert obligation['required_ranges']==[[1,3],[8,10]]
    blocks=list(conn.scalars(select(t.remedial_obligation_blocks.c.block_id)))
    tasks=conn.execute(select(t.study_tasks).where(t.study_tasks.c.block_id.in_(blocks))).mappings().all()
    assert tasks and all(x['scheduled_date']==c['nextday'] and 15<=x['estimated_minutes']<=60 for x in tasks)
    assert sum(x['estimated_minutes'] for x in tasks)==30
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==c['existing'])).mappings().one())==before
    assert all(x['ends_at']<=before['starts_at'] or x['starts_at']>=before['ends_at'] for x in tasks)
    ranges=conn.execute(select(t.study_task_ranges.c.start_page,t.study_task_ranges.c.end_page).where(t.study_task_ranges.c.task_id.in_([x['id'] for x in tasks]))).all()
    assert sorted(ranges)==[(1,3),(8,10)]
    conn.commit()
    assert api_client.post(f'/api/quiz/{quiz}/submit',headers=c['headers'],json=body).json()['data']==result
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==1
    assert set(conn.scalars(select(t.remedial_obligation_blocks.c.block_id)))==set(blocks)


@pytest.mark.parametrize('blocked',['exam','busy','no_initial','invalid_initial'])
def test_unplaceable_obligation_survives_but_unearned_pass_never_creates_it(api_client,domain_connection,remedial_case,blocked):
    c,conn=remedial_case,domain_connection
    if blocked=='exam':conn.execute(update(t.subjects).where(t.subjects.c.id==c['subject_id']).values(exam_date=DAY+timedelta(days=1)))
    if blocked=='busy':
        conn.execute(t.calendar_events.insert().values(id=str(uuid4()),user_id=c['user'].id,event_type='other',title='전일 일정',starts_at=at(c['nextday'],'08:00'),ends_at=at(c['nextday'],'23:00'),revision=1,created_at=AT,updated_at=AT))
    if blocked=='no_initial':conn.execute(t.learning_passes.delete().where(t.learning_passes.c.id==c['initial']))
    if blocked=='invalid_initial':conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==c['record']).values(end_page=t.study_record_ranges.c.start_page))
    conn.commit();submit(api_client,conn,c)
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==(0 if 'initial' in blocked else 1)
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligation_blocks))==0
    assert conn.scalar(select(func.count()).select_from(t.study_tasks).where(t.study_tasks.c.task_type=='remedial'))==0


def test_clearing_time_can_schedule_preserved_obligation_without_duplicate_blocks(api_client,domain_connection,remedial_case):
    c,conn=remedial_case,domain_connection
    event=str(uuid4());conn.execute(t.calendar_events.insert().values(id=event,user_id=c['user'].id,event_type='other',title='막힌 날',starts_at=at(c['nextday'],'08:00'),ends_at=at(c['nextday'],'23:00'),revision=1,created_at=AT,updated_at=AT));conn.commit()
    submit(api_client,conn,c);identifier=conn.scalar(select(t.remedial_obligations.c.id))
    conn.execute(update(t.calendar_events).where(t.calendar_events.c.id==event).values(deleted_at=AT))
    assert schedule_obligation(conn,c['user'].to_mapping(),identifier,AT)
    count=conn.scalar(select(func.count()).select_from(t.remedial_obligation_blocks))
    assert not schedule_obligation(conn,c['user'].to_mapping(),identifier,AT)
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligation_blocks))==count


def test_quiz_before_initial_completion_creates_obligation_when_pass_becomes_valid(api_client,domain_connection,remedial_case):
    from app.services.remedial import after_initial_passes
    c,conn=remedial_case,domain_connection
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id==c['initial']).values(invalidated_at=AT));conn.commit()
    submit(api_client,conn,c)
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==0
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id==c['initial']).values(invalidated_at=None))
    after_initial_passes(conn,c['user'].id,[c['units'][0]['id']],AT)
    after_initial_passes(conn,c['user'].id,[c['units'][0]['id']],AT)
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==1


def test_large_obligation_splits_blocks_without_splitting_obligation_identity(api_client,domain_connection,remedial_case):
    c,conn=remedial_case,domain_connection;uid=c['units'][0]['id']
    concept=conn.scalar(select(t.concepts.c.id).where(t.concepts.c.unit_id==uid,t.concepts.c.sort_order==1))
    conn.execute(update(t.units).where(t.units.c.id==uid).values(end_page=50))
    conn.execute(update(t.concepts).where(t.concepts.c.id==concept).values(end_page=50))
    conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==c['record']).values(end_page=50))
    metrics=QuizService(conn,c['user'].to_mapping(),DAY)._metrics_input();unit=next(x for x in metrics['units'] if x['id']==uid)
    snapshot=dict(conn.scalar(select(t.learning_passes.c.evidence_snapshot).where(t.learning_passes.c.id==c['initial'])))
    snapshot.update(requiredRanges=[[1,50]],masteryScopeRevision=current_scope(unit,metrics['concepts']).revision)
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id==c['initial']).values(evidence_snapshot=snapshot));conn.commit()
    submit(api_client,conn,c)
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==1
    blocks=list(conn.scalars(select(t.remedial_obligation_blocks.c.block_id)))
    rows=conn.execute(select(t.study_tasks).where(t.study_tasks.c.block_id.in_(blocks))).mappings().all()
    assert len(blocks)>=5 and all(15<=row['estimated_minutes']<=60 for row in rows)
    assert sum(row['estimated_minutes'] for row in rows)==250
    assert all(row['scheduled_date']==c['nextday'] for row in rows)


@pytest.mark.parametrize('has_record',[True,False])
def test_full_relearn_can_be_first_pass_only_with_actual_record_proof(api_client,domain_connection,remedial_case,has_record):
    c,conn=remedial_case,domain_connection
    snapshot=dict(conn.scalar(select(t.learning_passes.c.evidence_snapshot).where(t.learning_passes.c.id==c['initial'])))
    snapshot['directRequestHash']='a'*64
    if not has_record:snapshot['recordIds']=[]
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id==c['initial']).values(completion_kind='full_relearn',evidence_key='direct:'+'b'*64,evidence_snapshot=snapshot));conn.commit()
    submit(api_client,conn,c)
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==int(has_record)


def test_allocation_snapshot_keeps_exact_original_speed_when_subject_settings_change(api_client,domain_connection,remedial_case):
    from fractions import Fraction
    c,conn=remedial_case,domain_connection
    conn.execute(update(t.subjects).where(t.subjects.c.id==c['subject_id']).values(minutes_per_page=2.5))
    conn.execute(update(t.units).where(t.units.c.id==c['units'][0]['id']).values(difficulty='mid'));conn.commit()
    quiz,body,result=submit(api_client,conn,c)
    links=[dict(row) for row in conn.execute(select(t.remedial_obligation_blocks)).mappings()]
    assert links
    for link in links:
        snapshot=link['allocation_snapshot']
        task=conn.execute(select(t.study_tasks).where(t.study_tasks.c.block_id==link['block_id'])).mappings().one()
        assert snapshot['schemaVersion']=='remedial-allocation-v1' and snapshot['taskId']==task['id']
        assert snapshot['date']==task['scheduled_date'].isoformat()
        duration=Fraction(*snapshot['end'])-Fraction(*snapshot['start'])
        assert duration==sum((part['to']-part['from']+1)*Fraction(*part['baseline'])*Fraction(*part['multiplier']) for part in snapshot['parts'])
        assert all(part['baseline']==[5,2] and part['multiplier']==[6,5] for part in snapshot['parts'])
    conn.execute(update(t.subjects).where(t.subjects.c.id==c['subject_id']).values(minutes_per_page=7))
    conn.execute(update(t.units).where(t.units.c.id==c['units'][0]['id']).values(difficulty='high'));conn.commit()
    assert api_client.post(f'/api/quiz/{quiz}/submit',headers=c['headers'],json=body).json()['data']==result
    assert [dict(row) for row in conn.execute(select(t.remedial_obligation_blocks)).mappings()]==links


def test_allocation_failure_rolls_back_new_facts_and_submit_retry_creates_one_obligation(api_client,domain_connection,remedial_case,monkeypatch):
    from sqlalchemy.engine import Connection
    c,conn=remedial_case,domain_connection
    quiz,items=make_quiz(conn,c,c['questions'][0],day=DAY)
    body={'quizId':quiz,'answers':[{'questionId':row['id'],'choice':1,'confidence':'sure'} for row in items]}
    original=Connection.execute
    def fail(self,statement,*args,**kwargs):
        if getattr(statement,'is_insert',False) and getattr(getattr(statement,'table',None),'name',None)=='remedial_obligation_blocks':
            raise RuntimeError('forced allocation persistence failure')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Connection,'execute',fail)
    failed=api_client.post(f'/api/quiz/{quiz}/submit',headers=c['headers'],json=body)
    assert failed.status_code==500
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==0
    assert conn.scalar(select(func.count()).select_from(t.mastery_history).where(t.mastery_history.c.quiz_session_id==quiz))==0
    assert conn.scalar(select(func.count()).select_from(t.quiz_attempts).join(t.quiz_items,t.quiz_attempts.c.quiz_item_id==t.quiz_items.c.id).where(t.quiz_items.c.quiz_session_id==quiz))==0
    conn.commit();monkeypatch.setattr(Connection,'execute',original)
    retry=api_client.post(f'/api/quiz/{quiz}/submit',headers=c['headers'],json=body)
    assert retry.status_code==200,retry.text
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==1


def test_previous_day_study_interval_still_blocks_its_overlap_with_next_study_day(api_client,domain_connection,remedial_case):
    c,conn=remedial_case,domain_connection
    sid,block,task=(str(uuid4()) for _ in range(3))
    conn.execute(t.subjects.insert().values(id=sid,user_id=c['user'].id,code='other',name='다른 과목',color='blue',status='active',exam_date=DAY+timedelta(days=4),created_at=AT,updated_at=AT))
    conn.execute(t.study_blocks.insert().values(id=block,subject_id=sid,task_type='study',created_at=AT))
    conn.execute(t.study_tasks.insert().values(id=task,plan_id=c['plan'],subject_id=sid,block_id=block,task_type='study',title='과거 자정 겹침',scheduled_date=c['nextday']-timedelta(days=1),
        starts_at=at(c['nextday']-timedelta(days=1),'22:00'),ends_at=at(c['nextday'],'09:00'),estimated_minutes=60,status='planned',created_at=AT));conn.commit()
    submit(api_client,conn,c)
    rows=conn.execute(select(t.study_tasks).where(t.study_tasks.c.task_type=='remedial')).mappings().all()
    assert rows and all(row['starts_at']>=at(c['nextday'],'09:30').replace(tzinfo=row['starts_at'].tzinfo) for row in rows)


def test_generated_remedial_records_create_one_verified_recovery_pass(api_client,domain_connection,remedial_case):
    from app.services.recovery_evidence import recovery_eligibility
    c,conn=remedial_case,domain_connection
    quiz,_,_=submit(api_client,conn,c)
    obligation=conn.execute(select(t.remedial_obligations)).mappings().one()
    tasks=list(conn.scalars(select(t.study_tasks.c.id).where(t.study_tasks.c.task_type=='remedial')))
    assert tasks
    conn.execute(update(t.users).where(t.users.c.id==c['user'].id).values(demo_date=c['nextday']));conn.commit()
    for task_id in tasks:
        response=api_client.post('/api/records',headers=c['headers'],json={'taskId':task_id,'result':'done'})
        assert response.status_code==200,response.text
    passes=conn.execute(select(t.learning_passes).where(t.learning_passes.c.completion_kind=='remedial')).mappings().all()
    assert len(passes)==1
    assert passes[0]['evidence_key']==obligation['id']
    assert passes[0]['evidence_snapshot']['sourceQuizId']==quiz
    metrics=QuizService(conn,c['user'].to_mapping(),c['nextday'])._metrics_input()
    eligible=recovery_eligibility(conn,c['user'].id,metrics)
    assert sum(row.supplement_completed for row in eligible)==1
    conn.commit()
    replay=api_client.post('/api/records',headers=c['headers'],json={'taskId':tasks[-1],'result':'done'})
    assert replay.status_code==200,replay.text
    assert conn.scalar(select(func.count()).select_from(t.learning_passes).where(t.learning_passes.c.completion_kind=='remedial'))==1
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligations))==1
