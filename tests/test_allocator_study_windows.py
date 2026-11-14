from dataclasses import replace
from datetime import timedelta
from fractions import Fraction
import pytest
from app.domain.ranges import PageSpan
from app.domain.scheduling import AllocationInput, BusyInterval, StudyWindow
from app.services.allocator import allocate
from tests.test_allocator import TODAY, subject, unit


def request(*windows, pages=12, busy=()):
    return AllocationInput(TODAY, (subject(days=3),), (unit(pages=pages),), busy=busy, study_windows=windows)


def selected(result, start, end):
    return [(task.day, page) for task in result.placements for part in task.parts
        for page in range(part.span.start, part.span.end+1) if start<=page<=end]


def test_short_selected_tail_can_share_a_task_with_unrestricted_old_pages():
    day=TODAY+timedelta(days=1)
    result=allocate(request(StudyWindow('add',(PageSpan('pdf',11,12),),(day,),5)))
    assert result.feasible
    assert selected(result,11,12)==[(day,11),(day,12)]
    mixed=next(task for task in result.placements if any(part.span.end==12 for part in task.parts))
    assert mixed.minutes>=15 and len(mixed.parts)>=2
    assert sorted(page for _,page in selected(result,1,12))==list(range(1,13))


def test_daily_limit_spans_multiple_units_and_multiple_free_intervals():
    windows=(StudyWindow('add',(PageSpan('pdf',1,12),),(TODAY,TODAY+timedelta(days=1)),15),)
    req=replace(request(*windows), units=(unit('first',pages=6),unit('second',pages=6,start=7)),
        busy=(BusyInterval(TODAY,495,510),BusyInterval(TODAY,525,1380)))
    result=allocate(req)
    assert result.feasible
    totals={day:sum(task.minutes for task in result.placements if task.day==day) for day in windows[0].dates}
    assert totals=={TODAY:15,TODAY+timedelta(days=1):15}


def test_weekday_and_period_holes_cannot_be_used_even_when_capacity_is_free():
    day=TODAY+timedelta(days=1)
    result=allocate(request(StudyWindow('add',(PageSpan('pdf',1,12),),(day,),15)))
    assert not result.feasible and result.placements==()
    empty=allocate(request(StudyWindow('add',(PageSpan('pdf',1,12),),())))
    assert not empty.feasible


def test_time_limit_uses_exact_fraction_not_display_rounding():
    req=request(StudyWindow('add',(PageSpan('pdf',1,6),),(TODAY,),15),pages=6)
    assert allocate(req).feasible
    req=replace(req,subjects=(subject(days=3,speed=Fraction(501,200)),))
    assert not allocate(req).feasible


def test_constraints_do_not_leak_to_another_material_or_unselected_pages():
    day=TODAY+timedelta(days=1)
    req=request(StudyWindow('add',(PageSpan('pdf',1,6),),(day,),15),pages=6)
    req=replace(req,units=(*req.units,unit('other',material='second',pages=6)))
    result=allocate(req)
    assert result.feasible
    assert all(task.day==day for task in result.placements if any(part.span.material_id=='pdf' for part in task.parts))
    assert sum(part.span.end-part.span.start+1 for task in result.placements for part in task.parts)==12


@pytest.mark.parametrize('cap',[0,-1,False,float('nan')])
def test_invalid_caps_are_rejected(cap):
    with pytest.raises(ValueError): StudyWindow('add',(PageSpan('pdf',1,6),),(TODAY,),cap)


def test_overlapping_windows_and_duplicate_ids_are_rejected():
    window=StudyWindow('add',(PageSpan('pdf',1,6),),(TODAY,))
    with pytest.raises(ValueError): allocate(request(window,window))
    with pytest.raises(ValueError): allocate(request(window,replace(window,id='other')))


def test_no_window_preserves_existing_deterministic_result():
    req=request()
    assert allocate(req)==allocate(replace(req,study_windows=()))
