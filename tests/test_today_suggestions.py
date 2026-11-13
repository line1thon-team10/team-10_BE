from sqlalchemy import update

from app.db import schema as t


def test_tutor_suggestions_follow_actual_subjects_and_units(api_client, demo_user, domain_connection):
    headers = {"X-Anonymous-Id": demo_user}
    result = api_client.get("/api/today",headers=headers).json()["data"]
    assert result["tutorSuggestions"] == [
        "인공지능의 지도학습 알고리즘 핵심 개념을 설명해 줘.",
        "데이터베이스의 SQL 조회와 조작 핵심 개념을 설명해 줘.",
        "운영체제의 CPU 스케줄링 핵심 개념을 설명해 줘.",
    ]
    domain_connection.execute(update(t.subjects).where(t.subjects.c.code == "AI").values(name="기계학습"))
    domain_connection.execute(update(t.units).where(t.units.c.code == "AI2").values(title="분류 알고리즘"))
    domain_connection.commit()
    result = api_client.get("/api/today",headers=headers).json()["data"]
    assert result["tutorSuggestions"][0] == "기계학습의 분류 알고리즘 핵심 개념을 설명해 줘."
