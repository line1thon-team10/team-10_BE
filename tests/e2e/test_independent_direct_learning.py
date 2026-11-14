"""Independent direct-relearning book projection through record corrections."""
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_planless_review_correction import direct_record, fixed_rows
from tests.test_record_transactions import headers


def test_planless_book_follows_record_corrections_without_becoming_a_plan_task(api_client, demo_user, domain_connection):
    conn = domain_connection
    template, direct, rec, full = direct_record(conn, demo_user)
    uid = template.ranges[0]['unit_id']
    block = conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id == direct))
    original_pass = dict(conn.execute(select(t.learning_passes).where(t.learning_passes.c.id == full)).mappings().one())
    protected = fixed_rows(conn)
    conn.commit()
    def view():
        response = api_client.get('/api/books', headers=headers(demo_user))
        assert response.status_code == 200, response.text
        units = [u for s in response.json()['data']['subjects'] for u in s['units'] if u['id'] == uid]
        return next(b for b in units[0]['blocks'] if b['id'] == block)
    assert view()['state'] in ('done', 'risk', 'shaky', 'stable')
    for body, expected in (({'toPage': 13}, 'doing'), ({'completedRanges': []}, 'planned'), ({'toPage': 20}, 'done')):
        response = api_client.patch(f'/api/tasks/{direct}/record', headers=headers(demo_user), json=body)
        assert response.status_code == 200, response.text
        if expected == 'done':
            assert view()['state'] in ('done', 'risk', 'shaky', 'stable')
        else:
            assert view()['state'] == expected
        assert conn.scalar(select(t.study_tasks.c.plan_id).where(t.study_tasks.c.id == direct)) is None
        assert fixed_rows(conn) == protected
        conn.commit()
    assert dict(conn.execute(select(t.learning_passes).where(t.learning_passes.c.id == full)).mappings().one()) == original_pass


