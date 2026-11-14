from dataclasses import replace
from fractions import Fraction
import pytest
from app.domain.ranges import PageSpan
from app.domain.scheduling import AllocationInput, FixedTask, FixedRange, ScopePart, WorkRequirement, StudyWindow
from app.services.allocator import allocate
from tests.test_allocator import TODAY, subject, unit


def test_compression_changes_only_new_study_expected_time():
    request=AllocationInput(TODAY,(subject(),),(unit(pages=12),),requirements=(
        WorkRequirement('owed','a','remedial',(ScopePart('u',PageSpan('pdf',1,6)),),indivisible=True),),
        study_time_factor=Fraction(7,10))
    result=allocate(request)
    assert result.feasible
    assert sum(task.minutes for task in result.placements if task.kind=='study')==21
    assert sum(task.minutes for task in result.placements if task.kind=='remedial')==15
    assert all(part.baseline_minutes_per_page==Fraction(5,2) for task in result.placements for part in task.parts)
    assert all(part.difficulty_multiplier==Fraction(7,10) for task in result.placements if task.kind=='study' for part in task.parts)
    assert all(part.difficulty_multiplier==1 for task in result.placements if task.kind=='remedial' for part in task.parts)


def test_compression_does_not_shrink_fixed_tasks_or_relax_minimum_task_size():
    fixed=FixedTask('started','a','study','started',TODAY,480,510,(FixedRange('u',PageSpan('pdf',1,12)),))
    request=AllocationInput(TODAY,(subject(),),(unit(pages=18),),fixed=(fixed,),study_time_factor=Fraction(7,10))
    result=allocate(request)
    assert not result.feasible and result.fixed==(fixed,)


def test_window_caps_use_actual_compressed_expected_minutes():
    request=AllocationInput(TODAY,(subject(),),(unit(pages=12),),study_time_factor=Fraction(7,10),
        study_windows=(StudyWindow('selected',(PageSpan('pdf',1,12),),(TODAY,),21),))
    assert allocate(request).feasible
    assert not allocate(replace(request,study_time_factor=Fraction(1))).feasible


@pytest.mark.parametrize('factor',[0,-1,True,1.01,'0.7',float('nan')])
def test_compression_rejects_zero_growth_and_coerced_input(factor):
    with pytest.raises(ValueError): AllocationInput(TODAY,(subject(),),(unit(),),study_time_factor=factor)
