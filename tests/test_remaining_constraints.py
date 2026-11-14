"""Real SQLite/PostgreSQL DDL, preflight and historical data preservation."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import MetaData, inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.db import schema as t
from app.db.migrations import migration_config
from tests.test_quiz_storage_migration import seed_legacy, rows

CONTRACT=json.loads((Path(__file__).parents[1]/'schema/contract-invariants.json').read_text())
PREVIOUS='0010_remedial_obligations'


def populated(conn):
    command.upgrade(migration_config(conn),PREVIOUS)
    user=seed_legacy(conn)
    now=datetime(2026,11,14,tzinfo=timezone.utc)
    subject=conn.scalar(select(t.subjects.c.id).where(t.subjects.c.user_id==user))
    material=conn.scalar(select(t.materials.c.id).where(t.materials.c.subject_id==subject))
    unit=conn.scalar(select(t.units.c.id).where(t.units.c.material_id==material))
    quiz=conn.scalar(select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.user_id==user))
    item=conn.scalar(select(t.quiz_items.c.id).where(t.quiz_items.c.quiz_session_id==quiz))
    question=conn.scalar(select(t.quiz_items.c.question_id).where(t.quiz_items.c.id==item))
    def missing(table,**values):
        if conn.execute(select(table).limit(1)).first() is None:
            if 'id' in table.c: values['id']=str(uuid4())
            conn.execute(table.insert().values(**values))
    missing(t.onboarding_messages,user_id=user,subject_id=subject,role='ai',text='history',sort_order=0,created_at=now)
    missing(t.mastery_history,unit_id=unit,quiz_session_id=quiz,raw_score=70,score=70,level='shaky',
        recovery_mode=False,recorded_date=now.date(),created_at=now)
    missing(t.learning_passes,unit_id=unit,pass_number=1,completion_kind='initial',evidence_key='original',
        evidence_snapshot={},completed_at=now,completed_date=now.date())
    missing(t.quiz_scopes,quiz_session_id=quiz,unit_id=unit,start_page=1,end_page=2)
    missing(t.quiz_attempts,quiz_item_id=item,answer='history',confidence='sure',is_correct=True,
        grading_status='graded',answered_at=now,answered_date=now.date())
    missing(t.weak_questions,question_id=question,status='active',review_step=0,last_weak_at=now)
    missing(t.speed_calibrations,subject_id=subject,notice_date=now.date(),window_start=now.date(),window_end=now.date(),
        measured_seconds=100,completed_pages=5,previous_minutes_per_page=2,proposed_minutes_per_page=1,status='pending',created_at=now)
    missing(t.tutor_messages,user_id=user,subject_id=subject,kind='basic',question='q',answer='a',created_at=now,answered_at=now)
    message=conn.scalar(select(t.tutor_messages.c.id))
    missing(t.tutor_sources,message_id=message,material_id=material,start_page=1,end_page=2)
    missing(t.reflection_reports,user_id=user,period_start=now.date(),period_end=now.date(),status='ready',input_snapshot={},created_at=now)
    missing(t.ai_jobs,user_id=user,kind='material',status='queued',idempotency_key='old',input_hash='a'*64,
        input_payload={},schema_version='1',retry_count=0,created_at=now)
    missing(t.ai_usage_logs,user_id=user,kind='material',provider='test',model='test',status='succeeded',created_at=now)
    event=conn.scalar(select(t.calendar_events.c.id))
    missing(t.calendar_event_exceptions,event_id=event,occurrence_date=now.date(),kind='cancelled',created_at=now,updated_at=now)
    conn.commit()
    return user


def historical_schema(conn):
    schema = MetaData()
    schema.reflect(conn, only=lambda name, _: name != 'alembic_version')
    return schema


def snapshot(conn, schema=None):
    schema = historical_schema(conn) if schema is None else schema
    return {name: rows(conn, table) for name, table in schema.tables.items()}


def test_populated_upgrade_downgrade_preserves_every_row_and_schema(migration_connection):
    conn=migration_connection;populated(conn)
    # Compare every original column across upgrades that add new nullable columns.
    # The separate metadata comparison still validates the complete current schema.
    original_schema=historical_schema(conn)
    before=snapshot(conn,original_schema);conn.commit()
    config=migration_config(conn)
    for _ in range(2):
        command.upgrade(config,'head')
        assert snapshot(conn,original_schema)==before
        assert compare_metadata(MigrationContext.configure(conn),t.metadata)==[]
        inspector=inspect(conn)
        for check in CONTRACT['checks']:
            assert check['name'] in {c['name'] for c in inspector.get_check_constraints(check['table'])}
        conn.commit();command.downgrade(config,PREVIOUS)
        assert snapshot(conn,original_schema)==before
        for check in CONTRACT['checks']:
            assert check['name'] not in {c['name'] for c in inspect(conn).get_check_constraints(check['table'])}
        if conn.dialect.name=='sqlite':
            assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar()==1
        conn.commit()


@pytest.mark.parametrize('kind',['unit_order','concept_order','concept_code','active_block','range','negative','score','interval','half_null'])
def test_bad_legacy_data_stops_before_any_ddl_without_repair(migration_connection,kind):
    conn=migration_connection;populated(conn)
    if kind in ('unit_order','concept_order','concept_code','active_block'):
        table={'unit_order':t.units,'concept_order':t.concepts,'concept_code':t.concepts,'active_block':t.study_tasks}[kind]
        query=select(table)
        if kind=='active_block': query=query.where(table.c.status.in_(('planned','partial')))
        row=dict(conn.execute(query).mappings().first());row['id']=str(uuid4())
        if kind=='concept_order': row['code']='different-code'
        if kind=='concept_code': row['sort_order']+=999
        conn.execute(table.insert().values(**row))
    elif kind=='range': conn.execute(update(t.units).values(start_page=10,end_page=5))
    elif kind=='negative': conn.execute(update(t.daily_plan_totals).values(planned_total_pages=-1))
    elif kind=='score': conn.execute(update(t.units).values(mastery_score=101))
    elif kind=='interval': conn.execute(update(t.calendar_events).values(ends_at=t.calendar_events.c.starts_at))
    else: conn.execute(update(t.study_tasks).values(starts_at=datetime.now(timezone.utc),ends_at=None))
    before=snapshot(conn);conn.commit()
    with pytest.raises(RuntimeError,match='기존 .* 행'):
        command.upgrade(migration_config(conn),'head')
    conn.rollback()
    assert snapshot(conn)==before
    assert MigrationContext.configure(conn).get_current_revision()==PREVIOUS
    for check in CONTRACT['checks']:
        assert check['name'] not in {c['name'] for c in inspect(conn).get_check_constraints(check['table'])}
    if conn.dialect.name=='sqlite': assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar()==1


def test_partial_uniqueness_allows_retired_and_historical_placements(migration_connection):
    conn=migration_connection;populated(conn)
    command.upgrade(migration_config(conn),'head')
    for table, historical in [(t.units,{'retired_at':datetime.now(timezone.utc)}),
            (t.concepts,{'retired_at':datetime.now(timezone.utc)}),(t.study_tasks,{'status':'carried_over'})]:
        query=select(table)
        if table is t.study_tasks: query=query.where(table.c.status.in_(('planned','partial')))
        row=dict(conn.execute(query).mappings().first())
        with pytest.raises(IntegrityError),conn.begin_nested():
            conn.execute(table.insert().values(**{**row,'id':str(uuid4())}))
        conn.execute(table.insert().values(**{**row,'id':str(uuid4()),**historical}))
    conn.commit()


def test_scalar_invariants_reject_invalid_updates_and_preserve_nullable_ranges(migration_connection):
    conn=migration_connection;populated(conn)
    command.upgrade(migration_config(conn),'head')
    cases=[(t.subjects,{'minutes_per_page':-1}),(t.subjects,{'target_score':101}),
        (t.calendar_events,{'revision':0}),(t.calendar_events,{'ends_at':t.calendar_events.c.starts_at}),
        (t.materials,{'page_count':-1}),(t.materials,{'ocr_confidence':1.01}),
        (t.units,{'start_page':0}),(t.units,{'end_page':None}),(t.units,{'estimated_minutes':-1}),
        (t.units,{'mastery_score':-1}),(t.concepts,{'end_page':0}),(t.concepts,{'sort_order':-1}),
        (t.plans,{'revision':0}),(t.study_tasks,{'estimated_minutes':-1}),
        (t.study_task_ranges,{'start_page':0}),(t.study_task_ranges,{'planned_minutes':-1}),
        (t.daily_plan_totals,{'planned_new_pages':-1}),(t.study_sessions,{'measured_seconds':-1}),
        (t.study_sessions,{'ended_at':datetime(2000,1,1,tzinfo=timezone.utc)}),
        (t.study_session_segments,{'ended_at':datetime(2000,1,1,tzinfo=timezone.utc)}),
        (t.study_session_segments,{'sort_order':-1}),(t.study_record_ranges,{'start_page':0}),
        (t.study_record_ranges,{'completed_planned_minutes':-1}),
        (t.mastery_history,{'score':101}),(t.mastery_history,{'daily_increase_limit':-1}),
        (t.learning_passes,{'pass_number':0}),(t.question_sources,{'start_page':0}),
        (t.quiz_sessions,{'current_position':-1}),(t.quiz_scopes,{'end_page':0}),
        (t.quiz_items,{'sort_order':-1}),(t.quiz_attempts,{'essay_score':101}),
        (t.weak_questions,{'review_step':-1}),
        (t.onboarding_messages,{'sort_order':-1}),
        (t.calendar_event_exceptions,{'kind':'moved','new_starts_at':None,'new_ends_at':None}),
        (t.speed_calibrations,{'measured_seconds':-1}),
        (t.speed_calibrations,{'window_start':datetime(2030,1,1).date()}),
        (t.tutor_sources,{'start_page':0}),
        (t.reflection_reports,{'period_end':datetime(2000,1,1).date()}),
        (t.ai_jobs,{'retry_count':-1}),(t.ai_usage_logs,{'latency_ms':-1})]
    for table,values in cases:
        assert conn.execute(select(table).limit(1)).first() is not None, table.name
        with pytest.raises(IntegrityError),conn.begin_nested():
            conn.execute(update(table).values(**values))
    # Legacy time-only rows remain representable; a zero-duration focus segment
    # represents an immediate pause/end and is not a backwards time interval.
    conn.execute(update(t.units).values(start_page=None,end_page=None,estimated_minutes=0))
    conn.execute(update(t.study_session_segments).values(ended_at=t.study_session_segments.c.started_at))
    conn.commit()


def test_migration_uses_frozen_manifest():
    frozen=Path(__file__).parents[1]/'migrations/baselines/contract_invariants_0011.json'
    assert json.loads(frozen.read_text())==CONTRACT
