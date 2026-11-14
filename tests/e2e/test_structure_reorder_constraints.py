"""Existing structure identities must survive legal number/order exchanges."""
from sqlalchemy import select
from app.db import schema as t
from tests.test_units_contract import unit_case, add_unit, add_concept
from tests.test_material_structure_e2e import body_for, put


def test_exchange_unit_numbers_and_concept_order_preserves_identity(api_client,domain_connection,unit_case):
    conn,case=domain_connection,unit_case
    a=add_unit(conn,case,mastery_score=81)
    b=add_unit(conn,case,no=2,start=13,end=30,order=2,mastery_score=72)
    x=add_concept(conn,a,start=1,end=5,order=1)
    y=add_concept(conn,a,start=6,end=12,order=2)
    conn.commit()
    body=body_for(api_client,case)
    body['units'][0]['no']=2
    body['units'][1]['no']=1
    body['units'][0]['concepts'].reverse()
    response=put(api_client,case,body)
    assert response.status_code==200,response.text
    units=response.json()['data']['structure']['units']
    assert [row['id'] for row in units]==[b,a]
    assert [row['id'] for row in units[1]['concepts']]==[y,x]
    assert conn.scalar(select(t.units.c.mastery_score).where(t.units.c.id==a))==81
    assert conn.scalar(select(t.units.c.mastery_score).where(t.units.c.id==b))==72
    assert all(row is None for row in conn.scalars(select(t.units.c.retired_at)))
    assert all(row is None for row in conn.scalars(select(t.concepts.c.retired_at)))


def test_failed_reorder_rolls_back_temporary_retirement_and_never_revives_history(api_client,domain_connection,unit_case,monkeypatch):
    from datetime import datetime,timezone
    from app.services.units import UnitService
    conn,case=domain_connection,unit_case
    a=add_unit(conn,case,mastery_score=81)
    b=add_unit(conn,case,no=2,start=13,end=30,order=2,mastery_score=72)
    add_concept(conn,a)
    retired=add_unit(conn,case,mastery_score=35,retired_at=datetime(2020,1,1,tzinfo=timezone.utc))
    conn.commit()
    before=(conn.execute(select(t.units).order_by(t.units.c.id)).all(),conn.execute(select(t.concepts).order_by(t.concepts.c.id)).all())
    conn.commit()
    body=body_for(api_client,case)
    body['units'][0]['no']=2
    body['units'][1]['no']=1
    original=UnitService.get
    def fail(*args,**kwargs):
        raise RuntimeError('response failure after all structure writes')
    monkeypatch.setattr(UnitService,'get',fail)
    assert put(api_client,case,body).status_code==500
    assert (conn.execute(select(t.units).order_by(t.units.c.id)).all(),conn.execute(select(t.concepts).order_by(t.concepts.c.id)).all())==before
    conn.commit()
    monkeypatch.setattr(UnitService,'get',original)
    response=put(api_client,case,body)
    assert response.status_code==200,response.text
    assert {row['id'] for row in response.json()['data']['structure']['units']}=={a,b}
    assert conn.scalar(select(t.units.c.retired_at).where(t.units.c.id==retired)) is not None
    assert conn.scalar(select(t.units.c.mastery_score).where(t.units.c.id==retired))==35
