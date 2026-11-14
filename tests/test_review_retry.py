from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import func,select,update
from app.db import schema as t
from tests.test_quiz_answer_api import answer_case,make_case,DAY,NOW
from tests.test_review_notes import weak


def retry(client,case,kind='choice',**changes):
    payload={'choice':0,'text':None,'confidence':'sure'} if kind=='choice' else {'choice':None,'text':'','confidence':'sure'}
    return client.post(f"/api/review/notes/{case[kind]}/retry",headers={'X-Anonymous-Id':case['user']},json={**payload,**changes})


def test_retry_due_cycle_and_exact_response_replay(api_client,domain_connection,answer_case):
    conn,case=domain_connection,answer_case
    weak(conn,case,day=DAY-timedelta(days=1),due=DAY)
    first=retry(api_client,case)
    assert first.status_code==200,first.text
    data=first.json()['data']
    assert data['correct'] is True and data['note']['streak']==1 and data['wrongCountBefore']==1
    assert data['note']['canRetry'] is False
    snapshot=(conn.scalar(select(func.count()).select_from(t.quiz_attempts)),conn.scalar(select(func.count()).select_from(t.mastery_history)))
    conn.commit()
    assert retry(api_client,case).json()==first.json()
    assert (conn.scalar(select(func.count()).select_from(t.quiz_attempts)),conn.scalar(select(func.count()).select_from(t.mastery_history)))==snapshot
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=DAY+timedelta(days=3)));conn.commit()
    second=retry(api_client,case)
    assert second.status_code==200,second.text
    assert second.json()['data']['note']['result']=='resolved' and second.json()['data']['note']['streak']==2
    assert retry(api_client,case).json()==second.json()


def test_early_foreign_and_invalid_retry_do_not_write(api_client,domain_connection,answer_case):
    conn,case=domain_connection,answer_case
    weak(conn,case,due=DAY+timedelta(days=1))
    assert retry(api_client,case).json()['error']['code']=='REVIEW_NOT_DUE'
    conn.execute(update(t.weak_questions).where(t.weak_questions.c.question_id==case['choice']).values(next_review_date=DAY));conn.commit()
    before=conn.scalar(select(func.count()).select_from(t.quiz_sessions));conn.commit()
    assert retry(api_client,case,choice=True).status_code==422
    assert retry(api_client,case,choice=99).status_code==422
    assert conn.scalar(select(func.count()).select_from(t.quiz_sessions))==before
    other=make_case(conn);conn.commit()
    assert retry(api_client,{**case,'user':other['user']}).status_code==404


def test_wrong_retry_restarts_interval_and_stored_body_cannot_change(api_client,domain_connection,answer_case):
    conn,case=domain_connection,answer_case
    weak(conn,case,due=DAY)
    first=retry(api_client,case,choice=1)
    assert first.status_code==200,first.text
    note=first.json()['data']['note']
    assert note['wrongCount']==2 and note['streak']==0 and note['result']=='sureWrong'
    assert retry(api_client,case,choice=1).json()==first.json()
    assert retry(api_client,case,choice=0).status_code==409
    row=conn.execute(select(t.weak_questions).where(t.weak_questions.c.question_id==case['choice'])).mappings().one()
    assert row['next_review_date']==DAY+timedelta(days=1)


def test_essay_failure_keeps_original_answer_for_retry(api_client,domain_connection,answer_case,monkeypatch):
    from app.services import grading
    from app.core.responses import ApiError
    conn,case=domain_connection,answer_case
    weak(conn,case,kind='essay',due=DAY)
    original=grading.GradingService.run_job
    monkeypatch.setattr(grading.GradingService,'run_job',lambda *args,**kw: (_ for _ in ()).throw(ApiError('AI_FAILED','failure',503)))
    failed=retry(api_client,case,kind='essay',text='')
    assert failed.status_code==503,failed.text
    answers=conn.execute(select(t.quiz_answers)).mappings().all()
    assert len(answers)==1 and answers[0]['text']==''
    assert conn.scalar(select(func.count()).select_from(t.quiz_attempts))==1
    conn.commit();monkeypatch.setattr(grading.GradingService,'run_job',original)
    response=retry(api_client,case,kind='essay',text='')
    assert response.status_code==200,response.text
    assert conn.scalar(select(func.count()).select_from(t.quiz_answers))==1
    assert conn.scalar(select(func.count()).select_from(t.quiz_attempts))==2


def test_response_receipt_failure_rolls_back_score_and_review_step(api_client,domain_connection,answer_case,monkeypatch):
    from app.repositories.record_requests import RecordRequestRepository
    conn,case=domain_connection,answer_case;weak(conn,case,due=DAY)
    original=RecordRequestRepository.save
    def fail(self,response):
        if self.identity['path'].endswith('/retry'):
            raise RuntimeError('lost receipt write')
        return original(self,response)
    monkeypatch.setattr(RecordRequestRepository,'save',fail)
    response=retry(api_client,case)
    assert response.status_code==500,response.text
    assert conn.scalar(select(func.count()).select_from(t.mastery_history))==0
    assert conn.scalar(select(t.weak_questions.c.review_step).where(t.weak_questions.c.question_id==case['choice']))==0
    assert conn.scalar(select(func.count()).select_from(t.quiz_attempts))==1
    conn.commit();monkeypatch.setattr(RecordRequestRepository,'save',original)
    response=retry(api_client,case)
    assert response.status_code==200,response.text
    assert response.json()['data']['note']['streak']==1


def test_competing_retry_uses_one_sealed_answer(api_client,domain_connection,answer_case,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.services.grading import GradingService
    conn,case=domain_connection,answer_case;weak(conn,case,due=DAY)
    entered,release=Event(),Event();original=GradingService.run_job
    def paused(self,*args,**kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(GradingService,'run_job',paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first=executor.submit(retry,api_client,case)
        assert entered.wait(10)
        try:
            second=retry(api_client,case)
            assert second.status_code in (200,503,409),second.text
        finally:release.set()
        completed=first.result(timeout=10)
        assert completed.status_code in (200,503,409),completed.text
    final=retry(api_client,case)
    assert final.status_code==200,final.text
    assert conn.scalar(select(func.count()).select_from(t.quiz_answers))==1
    assert conn.scalar(select(func.count()).select_from(t.mastery_history))==1
    assert conn.scalar(select(func.count()).select_from(t.quiz_attempts))==2
    assert final.json()['data']['note']['streak']==1


def test_unrelated_review_set_does_not_hijack_retry_identity(api_client,domain_connection,answer_case):
    conn,case=domain_connection,answer_case;weak(conn,case,due=DAY)
    other_quiz,item=str(uuid4()),str(uuid4())
    conn.execute(t.quiz_sessions.insert().values(id=other_quiz,user_id=case['user'],subject_id=case['subject'],type='review',
        quiz_date=DAY,status='ready',created_at=NOW))
    conn.execute(t.quiz_items.insert().values(id=item,quiz_session_id=other_quiz,question_id=case['choice'],sort_order=1,inclusion_reason='weak'))
    conn.commit()
    response=retry(api_client,case)
    assert response.status_code==200,response.text
    assert response.json()['data']['note']['streak']==1
    assert conn.scalar(select(t.quiz_sessions.c.status).where(t.quiz_sessions.c.id==other_quiz))=='ready'
    assert conn.scalar(select(func.count()).select_from(t.quiz_answers).where(t.quiz_answers.c.quiz_item_id==item))==0
