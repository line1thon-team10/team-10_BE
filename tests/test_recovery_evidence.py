"""HTTP mastery caps require independently checked current-scope evidence."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.ai.metrics_adapter import current_scope
from app.db import schema as t
from app.services.quiz import QuizService
from app.services.recovery_evidence import recovery_eligibility
from tests.test_mastery_rules import mastery_case, submit_quiz, make_quiz, DAY, AT


def add_direct(conn, case, *, day, unit_index=0, **changes):
    metrics=QuizService(conn,case['user'].to_mapping(),day)._metrics_input()
    unit=next(row for row in metrics['units'] if row['id']==case['units'][unit_index]['id'])
    snapshot={'schemaVersion':'1','materialId':unit['material_id'],'recordIds':[],
        'requiredRanges':[[1,5]],'masteryScopeRevision':current_scope(unit,metrics['concepts']).revision,
        'directRequestHash':'a'*64}
    snapshot.update(changes.pop('snapshot',{}))
    row=dict(id=str(uuid4()),unit_id=unit['id'],pass_number=1,completion_kind='full_relearn',
        evidence_key='direct:'+'b'*64,evidence_snapshot=snapshot,completed_at=AT,completed_date=day)
    row.update(changes);conn.execute(t.learning_passes.insert().values(**row));conn.commit()
    return row


def submit_with_scope(client,conn,case,*,day,scope=(1,5),kind='practice'):
    quiz,items=make_quiz(conn,case,case['questions'][0],day=day)
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==quiz).values(type=kind))
    if scope:
        conn.execute(t.quiz_scopes.insert().values(id=str(uuid4()),quiz_session_id=quiz,
            unit_id=case['units'][0]['id'],start_page=scope[0],end_page=scope[1]))
    conn.commit()
    body={'quizId':quiz,'answers':[{'questionId':x['id'],'choice':0,'confidence':'sure'} for x in items]}
    response=client.post(f'/api/quiz/{quiz}/submit',headers=case['headers'],json=body)
    assert response.status_code==200,response.text
    return quiz,response.json()['data'],body


def test_same_day_full_scope_custom_opens_fifteen_once_and_replay_preserves_result(api_client,domain_connection,mastery_case):
    c,conn=mastery_case,domain_connection
    submit_quiz(api_client,conn,c,c['questions'][0][:10],7)
    day=DAY+timedelta(days=1)
    conn.execute(update(t.users).where(t.users.c.id==c['user'].id).values(demo_date=day));conn.commit()
    add_direct(conn,c,day=day)
    quiz,result,body=submit_with_scope(api_client,conn,c,day=day)
    hist=conn.execute(select(t.mastery_history).where(t.mastery_history.c.quiz_session_id==quiz)).mappings().one()
    assert hist['daily_increase_limit']==15 and hist['score']==79  # no supplemental completion yet
    conn.commit()
    assert api_client.post(f'/api/quiz/{quiz}/submit',headers=c['headers'],json=body).json()['data']==result
    assert len(conn.execute(select(t.mastery_history).where(t.mastery_history.c.quiz_session_id==quiz)).all())==1


@pytest.mark.parametrize('bad',['other_day','other_unit','stale_scope','invalidated','partial_scope','no_scope','not_custom','missing_direct_hash'])
def test_relearning_and_custom_must_share_exact_day_unit_and_scope(api_client,domain_connection,mastery_case,bad):
    c,conn=mastery_case,domain_connection
    submit_quiz(api_client,conn,c,c['questions'][0][:10],7)
    day=DAY+timedelta(days=1)
    conn.execute(update(t.users).where(t.users.c.id==c['user'].id).values(demo_date=day));conn.commit()
    kw={}
    if bad=='stale_scope':kw['snapshot']={'masteryScopeRevision':'old'}
    if bad=='invalidated':kw['invalidated_at']=AT
    if bad=='missing_direct_hash':kw['snapshot']={'directRequestHash':None}
    add_direct(conn,c,day=DAY if bad=='other_day' else day,unit_index=1 if bad=='other_unit' else 0,**kw)
    quiz,_,_=submit_with_scope(api_client,conn,c,day=day,scope=(1,4) if bad=='partial_scope' else None if bad=='no_scope' else (1,5),kind='extra' if bad=='not_custom' else 'practice')
    hist=conn.execute(select(t.mastery_history).where(t.mastery_history.c.quiz_session_id==quiz)).mappings().one()
    assert hist['daily_increase_limit']==5 and hist['score']==75


def test_unsupported_pass_snapshot_does_not_raise_or_grant_bonus(api_client,domain_connection,mastery_case):
    c,conn=mastery_case,domain_connection
    add_direct(conn,c,day=DAY,snapshot={'recordIds':None})
    metrics=QuizService(conn,c['user'].to_mapping(),DAY)._metrics_input()
    assert recovery_eligibility(conn,c['user'].id,metrics)==()


def add_record_pass(conn,case,*,kind,day,source_quiz=None):
    unit=case['units'][0];ids={k:str(uuid4()) for k in ('block','task','record','range','pass','obligation')}
    plan=conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id==case['user'].id))
    if plan is None:
        plan=str(uuid4());conn.execute(t.plans.insert().values(id=plan,user_id=case['user'].id,revision=1,created_at=AT,approved_at=AT))
    conn.execute(t.study_blocks.insert().values(id=ids['block'],subject_id=case['subject_id'],unit_id=unit['id'],task_type='remedial' if kind=='remedial' else 'study',created_at=AT))
    conn.execute(t.study_tasks.insert().values(id=ids['task'],plan_id=plan,subject_id=case['subject_id'],block_id=ids['block'],task_type='remedial' if kind=='remedial' else 'study',title='실제 학습',scheduled_date=day,estimated_minutes=15,status='completed',created_at=AT))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=ids['task'],unit_id=unit['id'],start_page=1,end_page=5,sort_order=1))
    conn.execute(t.study_records.insert().values(id=ids['record'],task_id=ids['task'],result='completed',recorded_at=AT,study_date=day,updated_at=AT))
    conn.execute(t.study_record_ranges.insert().values(id=ids['range'],record_id=ids['record'],unit_id=unit['id'],start_page=1,end_page=5))
    metrics=QuizService(conn,case['user'].to_mapping(),day)._metrics_input(); current=next(row for row in metrics['units'] if row['id']==unit['id'])
    snapshot={'schemaVersion':'1','materialId':unit['material_id'],'recordIds':[ids['record']],
        'requiredRanges':[[1,5]],'masteryScopeRevision':current_scope(current,metrics['concepts']).revision}
    if kind=='remedial':
        conn.execute(t.remedial_obligations.insert().values(id=ids['obligation'],user_id=case['user'].id,unit_id=unit['id'],quiz_session_id=source_quiz,
            scope_revision=snapshot['masteryScopeRevision'],required_ranges=[[1,5]],not_before=day,created_at=AT))
        conn.execute(t.remedial_obligation_blocks.insert().values(obligation_id=ids['obligation'],block_id=ids['block']))
        snapshot.update(obligationId=ids['obligation'],blockIds=[ids['block']],sourceQuizId=source_quiz)
    conn.execute(t.learning_passes.insert().values(id=ids['pass'],unit_id=unit['id'],pass_number=2 if kind=='remedial' else 1,
        completion_kind=kind,source_task_id=ids['task'],evidence_key=ids['obligation'] if kind=='remedial' else 'initial',
        evidence_snapshot=snapshot,completed_at=AT,completed_date=day))
    conn.commit();return ids


def test_verified_remedial_requires_initial_and_live_complete_record_ranges(api_client,domain_connection,mastery_case):
    c,conn=mastery_case,domain_connection
    quiz,_=submit_quiz(api_client,conn,c,c['questions'][0][:10],7)
    initial=add_record_pass(conn,c,kind='initial',day=DAY)
    day=DAY+timedelta(days=1)
    remedial=add_record_pass(conn,c,kind='remedial',day=day,source_quiz=quiz)
    def eligible():
        metrics=QuizService(conn,c['user'].to_mapping(),day)._metrics_input()
        return recovery_eligibility(conn,c['user'].id,metrics)
    assert any(row.supplement_completed for row in eligible())
    conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.id==remedial['range']).values(end_page=4))
    assert not any(row.supplement_completed for row in eligible())
    conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.id==remedial['range']).values(end_page=5))
    assert any(row.supplement_completed for row in eligible())
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id==initial['pass']).values(invalidated_at=AT))
    assert not any(row.supplement_completed for row in eligible())
