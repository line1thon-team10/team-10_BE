from datetime import datetime, timezone
from uuid import uuid4
from alembic import command
from sqlalchemy import inspect, select
from app.db import schema as t
from app.db.migrations import migration_config


def test_remedial_migration_roundtrip_keeps_preexisting_user(migration_connection):
    conn=migration_connection; config=migration_config(conn)
    command.upgrade(config,'0009_record_requests')
    user=str(uuid4()); now=datetime.now(timezone.utc)
    conn.execute(t.users.insert().values(id=user,created_at=now,updated_at=now));conn.commit()
    command.upgrade(config,'0010_remedial_obligations')
    assert {'remedial_obligations','remedial_obligation_blocks'} <= set(inspect(conn).get_table_names())
    conn.commit();command.downgrade(config,'0009_record_requests')
    assert 'remedial_obligations' not in inspect(conn).get_table_names()
    assert conn.scalar(select(t.users.c.id))==user
    conn.commit();command.upgrade(config,'0010_remedial_obligations')
    assert conn.scalar(select(t.users.c.id))==user


def test_unique_obligation_links_and_owner_reset(domain_connection):
    from sqlalchemy.exc import IntegrityError
    import pytest
    from app.seed.loader import wipe_user
    conn=domain_connection;now=datetime.now(timezone.utc)
    rows=[]
    for index in range(2):
        ids={key:str(uuid4()) for key in ('user','subject','material','unit','quiz','block','obligation')}
        conn.execute(t.users.insert().values(id=ids['user'],created_at=now,updated_at=now))
        conn.execute(t.subjects.insert().values(id=ids['subject'],user_id=ids['user'],code='S',name='S',color='blue',status='active',created_at=now,updated_at=now))
        conn.execute(t.materials.insert().values(id=ids['material'],subject_id=ids['subject'],original_name='a.pdf',source_format='pdf',sha256='a'*64,analysis_version='1',created_at=now,updated_at=now))
        conn.execute(t.units.insert().values(id=ids['unit'],material_id=ids['material'],code='U',title='U',sort_order=1,start_page=1,end_page=5,created_at=now,updated_at=now))
        conn.execute(t.quiz_sessions.insert().values(id=ids['quiz'],user_id=ids['user'],subject_id=ids['subject'],type='practice',quiz_date=now.date(),status='ready',selected_difficulty='low',created_at=now))
        conn.execute(t.study_blocks.insert().values(id=ids['block'],subject_id=ids['subject'],unit_id=ids['unit'],task_type='remedial',created_at=now))
        obligation=dict(id=ids['obligation'],user_id=ids['user'],unit_id=ids['unit'],quiz_session_id=ids['quiz'],scope_revision='a'*64,required_ranges=[[1,5]],not_before=now.date(),created_at=now)
        conn.execute(t.remedial_obligations.insert().values(**obligation))
        conn.execute(t.remedial_obligation_blocks.insert().values(obligation_id=ids['obligation'],block_id=ids['block']))
        with pytest.raises(IntegrityError),conn.begin_nested():
            conn.execute(t.remedial_obligations.insert().values(**{**obligation,'id':str(uuid4())}))
        with pytest.raises(IntegrityError),conn.begin_nested():
            conn.execute(t.remedial_obligation_blocks.insert().values(obligation_id=str(uuid4()),block_id=ids['block']))
        rows.append(ids)
    conn.commit();wipe_user(conn,rows[0]['user'])
    assert list(conn.scalars(select(t.remedial_obligations.c.id)))==[rows[1]['obligation']]
    assert list(conn.scalars(select(t.remedial_obligation_blocks.c.block_id)))==[rows[1]['block']]
