from datetime import timedelta
from sqlalchemy import select
from app.db import schema as t
from app.services.quiz import QuizService
from tests.test_quiz_answer_api import answer_case, DAY, NOW
from tests.test_review_notes import weak


def test_early_correct_cannot_skip_review_intervals(domain_connection,answer_case):
    conn,case=domain_connection,answer_case
    weak(conn,case,day=DAY,due=DAY+timedelta(days=1))
    user=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    def state():return dict(conn.execute(select(t.weak_questions).where(t.weak_questions.c.question_id==case['choice'])).mappings().one())
    svc=QuizService(conn,user,DAY)
    before=state();svc._update_weak(case['choice'],True,NOW)
    assert state()==before
    svc.today=DAY+timedelta(days=1);svc._update_weak(case['choice'],True,NOW)
    assert state()['review_step']==1 and state()['next_review_date']==DAY+timedelta(days=4)
    before=state();svc._update_weak(case['choice'],True,NOW)
    assert state()==before
    svc.today=DAY+timedelta(days=4);svc._update_weak(case['choice'],True,NOW)
    assert state()['review_step']==2 and state()['status']=='resolved'
    svc._update_weak(case['choice'],False,NOW)
    assert state()['review_step']==0 and state()['status']=='active' and state()['next_review_date']==DAY+timedelta(days=5)
