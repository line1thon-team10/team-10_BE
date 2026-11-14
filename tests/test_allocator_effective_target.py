from datetime import timedelta
from fractions import Fraction
import pytest
from app.domain.scheduling import allocation_score
from tests.test_allocator import TODAY, subject


def test_goal_changes_only_on_effective_local_date():
    planned=subject(target=90,target_effective_date=TODAY+timedelta(days=1),previous_target_score=60)
    old=subject(target=60);new=subject(target=90)
    for day in (TODAY-timedelta(days=1),TODAY):
        assert allocation_score(planned,day,20,Fraction(1,2))==allocation_score(old,day,20,Fraction(1,2))
    for day in (TODAY+timedelta(days=1),TODAY+timedelta(days=2)):
        assert allocation_score(planned,day,20,Fraction(1,2))==allocation_score(new,day,20,Fraction(1,2))


@pytest.mark.parametrize('values',[{'target_effective_date':TODAY},{'previous_target_score':60},
    {'target_effective_date':TODAY,'previous_target_score':False},
    {'target_effective_date':TODAY,'previous_target_score':101},
    {'target_effective_date':'2026-11-14','previous_target_score':60}])
def test_scheduled_target_rejects_incomplete_or_coerced_metadata(values):
    with pytest.raises(ValueError): subject(**values)
