from uuid import uuid4

import pytest

from tests.test_focus_state import active, headers


@pytest.mark.parametrize("body",[{},[],"bad",{"taskId":None},{"taskId":123},{"taskId":"bad"},{"taskId":str(uuid4()),"minutes":42}])
def test_invalid_start_payload_returns_422_without_changing_focus(api_client,demo_user,body):
    before=active(api_client,demo_user)
    response=api_client.post("/api/study/sessions",headers=headers(demo_user),json=body)
    assert response.status_code==422
    assert active(api_client,demo_user)["id"]==before["id"]


@pytest.mark.parametrize("verb",["get","pause","resume","end"])
def test_malformed_or_foreign_session_id_is_rejected(api_client,demo_user,verb):
    suffix="" if verb=="get" else "/"+verb
    method=api_client.get if verb=="get" else api_client.post
    assert method("/api/study/sessions/bad"+suffix,headers=headers(demo_user)).status_code==422
    assert method(f"/api/study/sessions/{uuid4()}"+suffix,headers=headers(demo_user)).status_code==404
    other=api_client.post("/api/session").json()["data"]["anonymousId"]
    sid=active(api_client,other)["id"]
    assert method(f"/api/study/sessions/{sid}"+suffix,headers=headers(demo_user)).status_code==404


@pytest.mark.parametrize("verb",["pause","resume","end"])
def test_commands_accept_empty_body_but_reject_forged_time(api_client,demo_user,verb):
    sid=active(api_client,demo_user)["id"]
    url=f"/api/study/sessions/{sid}/{verb}"
    assert api_client.post(url,headers=headers(demo_user),json={"minutes":99}).status_code==422
    assert api_client.post(url,headers=headers(demo_user),json={}).status_code==200


def test_uuid_normalization_and_response_range_fields(api_client,demo_user):
    before=active(api_client,demo_user)
    response=api_client.post("/api/study/sessions",headers=headers(demo_user),json={"taskId":before["taskId"].upper()})
    assert response.status_code==200 and response.json()["data"]["id"]==before["id"]
    result=api_client.get(f"/api/study/sessions/{before['id'].replace('-','')}",headers=headers(demo_user))
    assert result.status_code==200
    r=result.json()["data"]["task"]["ranges"][0]
    assert set(r)=={"id","materialId","unitId","conceptId","from","to","doneTo"}
