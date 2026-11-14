from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.schemas.plan_additions import AdditionPreview


def body(**values):
    return {'subjectId':str(uuid4()),'materialId':str(uuid4()),'from':1,'to':20,
        'minutesPerDay':30,'days':[1,3,5],'startDate':'2026-11-16','endDate':'2026-11-20',**values}


@pytest.mark.parametrize('values', [ {'totalPages':10}, {'minutesPerDay':None}, {'minutesPerDay':0},
    {'minutesPerDay':True}, {'minutesPerDay':'30'}, {'minutesPerDay':None,'totalPages':21},
    {'days':[]},{'days':[1,1]},{'days':[0]},{'days':[8]},{'days':[True]}, {'from':0},
    {'from':21},{'to':True}, {'startDate':'2026-11-21'}, {'endDate':'2026-02-30'},
    {'subjectId':'foreign'}, {'extra':'ignored'}, {'startDate':'2026-1-1'}])
def test_invalid_contract_is_not_coerced(values):
    with pytest.raises(ValidationError): AdditionPreview.model_validate(body(**values))


def test_page_goal_is_exact_and_weekdays_canonical():
    command=AdditionPreview.model_validate(body(minutesPerDay=None,totalPages=3,days=[5,1,3]))
    assert command.totalPages==3 and command.days==[1,3,5]
