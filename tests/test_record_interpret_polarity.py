import pytest
from tests.test_progress_contract import progress_world
from tests.test_record_interpret import interpretation_case, interpret, snapshot


@pytest.mark.parametrize('text',['p.13까지 못 봤어','13쪽까지 안 보았어','13쪽까지 못 마쳤어',
    '13쪽까지 못다 읽었어','13쪽까지 안 풀었어','13쪽까지 안 읽고 완료라고 했어','절반은 못 읽었어','못 마쳤어'])
def test_negative_completion_forms_have_no_positive_candidate(api_client,interpretation_case,text):
    case=interpretation_case;conn=case['world'].conn
    before=snapshot(conn);conn.commit()
    response=interpret(api_client,case,text)
    assert response.status_code==400 and response.json()['error']['code']=='PARSE_FAILED',response.text
    assert case['model'].calls==0 and snapshot(conn)==before


@pytest.mark.parametrize('text',['아직 못 봤어','안 풀었어','못 읽었어'])
def test_unqualified_explicit_zero_remains_zero(api_client,interpretation_case,text):
    response=interpret(api_client,interpretation_case,text)
    assert response.status_code==200,response.text
    assert response.json()['data']['completedRanges']==[]
