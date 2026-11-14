import pytest
from sqlalchemy import update, select
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_record_interpret import interpretation_case,interpret,snapshot


def composite(case):
    w=case['world'];second=w.unit(10,20)
    w.conn.execute(update(t.materials).where(t.materials.c.id==second['material_id']).values(original_name='교재B.pdf'))
    w.conn.execute(update(t.units).where(t.units.c.id==second['id']).values(title='B단원'))
    case['task']=w.task((case['unit'],10,20),(second,10,20));w.conn.commit()


@pytest.mark.parametrize('text',['교재A.pdf 다 읽었어, 교재B.pdf는 아직',
    '교재A.pdf 다 읽었어 그리고 교재B.pdf는 나중에',
    '교재A.pdf 다 읽었어 교재B.pdf도',
    '교재A.pdf 다 읽었어, 교재B.pdf',
    '교재A.pdf 다 읽었어 교재B.pdf는 다음에'])
def test_one_completion_cannot_leak_into_another_clause(api_client,interpretation_case,text):
    case=interpretation_case;composite(case);conn=case['world'].conn
    before=snapshot(conn);conn.commit()
    response=interpret(api_client,case,text)
    assert response.status_code==400 and response.json()['error']['code']=='PARSE_FAILED',response.text
    assert case['model'].calls==0 and snapshot(conn)==before


def test_each_named_full_completion_can_be_confirmed_separately(api_client,interpretation_case):
    case=interpretation_case;composite(case);conn=case['world'].conn
    response=interpret(api_client,case,'교재A.pdf 다 읽었어, 교재B.pdf 모두 읽었어')
    assert response.status_code==200,response.text
    ids=list(conn.scalars(select(t.study_task_ranges.c.id).where(t.study_task_ranges.c.task_id==case['task']).order_by(t.study_task_ranges.c.sort_order)))
    assert response.json()['data']['completedRanges']==[{'rangeId':identifier,'toPage':20} for identifier in ids]
