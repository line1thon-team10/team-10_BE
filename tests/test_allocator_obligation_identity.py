from dataclasses import replace
from app.domain.ranges import PageSpan
from app.domain.scheduling import AllocationInput, ScopePart, WorkRequirement
from app.services.allocator import allocate
from tests.test_allocator import TODAY, subject, unit


def test_distinct_obligations_never_share_one_persisted_task():
    req=AllocationInput(TODAY,(subject(),),(replace(unit(),ranges=()),),requirements=(
        WorkRequirement('first','a','remedial',(ScopePart('u',PageSpan('pdf',1,6)),)),
        WorkRequirement('second','a','remedial',(ScopePart('u',PageSpan('pdf',7,12)),))))
    result=allocate(req)
    assert result.feasible and len(result.placements)==2
    assert all(len({part.requirement_id for part in task.parts})==1 for task in result.placements)
    assert {part.requirement_id for task in result.placements for part in task.parts}=={'first','second'}


def test_fragments_of_one_obligation_can_combine_across_units():
    req=AllocationInput(TODAY,(subject(),),(replace(unit(),ranges=()),replace(unit('other'),ranges=())),requirements=(
        WorkRequirement('single','a','review',(ScopePart('u',PageSpan('pdf',1,3)),ScopePart('other',PageSpan('pdf',4,6)))),))
    result=allocate(req)
    assert result.feasible and len(result.placements)==1 and len(result.placements[0].parts)==2
