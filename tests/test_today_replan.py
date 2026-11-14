from tests.test_plan_draft import plan_case
from tests.test_replan_options_e2e import recovery_case, options


def test_today_computes_actual_preview_and_suppresses_pace(api_client,domain_connection,recovery_case):
    case=recovery_case
    response=api_client.get('/api/today',headers=case['headers'])
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert value['paceAlert'] is None
    assert value['replan']['previewId']
    assert value['replan']['options']==options(api_client,case).json()['data']['options']
