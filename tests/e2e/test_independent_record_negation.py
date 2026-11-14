"""Negative completion language cannot become positive page evidence."""
import pytest
from sqlalchemy import update

from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_record_interpret import interpretation_case, interpret, snapshot


@pytest.mark.parametrize('text', ['13쪽까지 안 봤어', '13쪽까지 못 봤어', '13쪽까지 못 풀었어'])
def test_negative_page_claim_is_never_returned_as_completed(api_client, interpretation_case, text):
    case, conn = interpretation_case, interpretation_case['world'].conn
    before = snapshot(conn)
    conn.commit()
    response = interpret(api_client, case, text)
    assert response.status_code == 400 and response.json()['error']['code'] == 'PARSE_FAILED', response.text
    assert case['model'].calls == 0 and snapshot(conn) == before


def test_completion_in_first_clause_does_not_apply_to_postponed_other_pdf(api_client, interpretation_case):
    case = interpretation_case
    world = case['world']
    second = world.unit(10, 20)
    world.conn.execute(update(t.materials).where(t.materials.c.id == second['material_id']).values(original_name='교재B.pdf'))
    case['task'] = world.task((case['unit'], 10, 20), (second, 10, 20))
    world.conn.commit()
    before = snapshot(world.conn)
    world.conn.commit()
    response = interpret(api_client, case, '교재A.pdf 다 읽었어, 교재B.pdf는 다음에')
    assert response.status_code == 400 and response.json()['error']['code'] == 'PARSE_FAILED', response.text
    assert case['model'].calls == 0 and snapshot(world.conn) == before
