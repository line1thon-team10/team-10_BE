"""Follow-up candidates observe committed-domain state inside the same transaction."""
from dataclasses import replace
import pytest
from sqlalchemy import func, select, update
from app.db import schema as t
from app.repositories.proposals import ProposalRepository
from app.services.proposals import PlanEffect, ProposalService
from tests.test_proposal_storage import NOW, owner
from tests.test_proposals import TARGET, create, domain, plan


def test_followup_uses_new_revision_and_parent_replays_exact_response(domain_connection, owner):
    conn=domain_connection
    plan(conn,owner,revision=7)
    service=ProposalService(conn,clock=lambda:NOW)
    follow=domain(effect=PlanEffect.UPDATE,kind='followup')
    calls=[]
    def after(connection,context,response):
        assert context.plan.revision==8
        assert connection.scalar(select(t.plans.c.revision).where(t.plans.c.user_id==owner))==8
        calls.append(True)
        return {**response,'followup':create(service,owner,follow)['previewId']}
    operation=replace(domain(effect=PlanEffect.UPDATE),after_apply=after)
    parent=create(service,owner,operation)['previewId'];conn.commit()
    result=service.apply(owner,parent,domain=operation,target=TARGET);conn.commit()
    child=ProposalRepository(conn).get(owner,result['followup'])
    assert child['base_plan_revision']==8
    assert service.apply(owner,parent,domain=operation,target=TARGET)==result
    assert calls==[True]
    assert service.apply(owner,child['proposal_id'],domain=follow,target=TARGET)['applied']
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id==owner))==9


@pytest.mark.parametrize('failure',['exception','plan_write'])
def test_followup_failure_rolls_back_domain_revision_and_new_candidates(domain_connection, owner, failure):
    conn=domain_connection
    plan(conn,owner,revision=7)
    service=ProposalService(conn,clock=lambda:NOW)
    follow=domain(effect=PlanEffect.UPDATE,kind='followup')
    def after(connection,context,response):
        create(service,owner,follow)
        if failure=='exception':
            raise RuntimeError('injected follow-up failure')
        connection.execute(update(t.plans).where(t.plans.c.user_id==owner).values(revision=100))
        return response
    operation=replace(domain(effect=PlanEffect.UPDATE),after_apply=after)
    parent=create(service,owner,operation)['previewId'];conn.commit()
    with pytest.raises(Exception):
        service.apply(owner,parent,domain=operation,target=TARGET)
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id==owner))==7
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id==owner))=='draft'
    assert conn.scalar(select(func.count()).select_from(t.plan_proposals))==1
    assert ProposalRepository(conn).get(owner,parent)['status']=='ready'
