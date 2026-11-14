from datetime import timedelta
from uuid import uuid4
import pytest
from app.domain.ranges import PageSpan
from app.domain.scheduling import StudyWindow
from app.services.plan_inputs import PlanInputError, existing_allocation_input
from app.services.allocator import allocate
from tests.test_plan_draft import TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case


def prepare(conn,case,windows):
    return existing_allocation_input(conn,case['user'],today=TODAY,timezone_name='Asia/Seoul',
        subject_ids=(),affected_subject_ids=(case['subject'],),study_windows=windows)[0]


def test_owned_study_window_reaches_allocator(api_client,domain_connection,addition_case):
    case=addition_case
    allowed=TODAY+timedelta(days=1)
    window=StudyWindow('addition',(PageSpan(case['material'],1,10),),(allowed,),30)
    request=prepare(domain_connection,case,(window,))
    assert request.study_windows==(window,)
    result=allocate(request)
    assert result.feasible
    for placement in result.placements:
        if any(part.span.start<=10 for part in placement.parts):
            assert placement.day==allowed


@pytest.mark.parametrize('bad',['foreign_material','outside_scope','past','duplicate','untyped'])
def test_invalid_window_does_not_escape_owned_scope(api_client,domain_connection,addition_case,bad):
    case=addition_case
    window=StudyWindow('addition',(PageSpan(case['material'],1,10),),(TODAY,),30)
    windows=(window,)
    if bad=='foreign_material': windows=(StudyWindow('x',(PageSpan(str(uuid4()),1,10),),(TODAY,)),)
    if bad=='outside_scope': windows=(StudyWindow('x',(PageSpan(case['material'],20,21),),(TODAY,)),)
    if bad=='past': windows=(StudyWindow('x',window.ranges,(TODAY-timedelta(days=1),)),)
    if bad=='duplicate': windows=(window,window)
    if bad=='untyped': windows=({},)
    with pytest.raises(PlanInputError): prepare(domain_connection,case,windows)
