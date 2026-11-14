"""QA-03: real HTTP transactions and worker persistence from an empty account.

Only external AI transports and the controllable system clock are fixtures.
No service/repository mock and no direct writes of product data. This does not
claim real provider-key/quality verification.
"""
from datetime import date,datetime,timedelta,timezone
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import func,select
from app.db import schema as t


def data(response):
    assert response.status_code==200,response.text
    return response.json()['data']


@pytest.fixture(params=['ordinary','empty_demo'])
def journey(api_client,domain_connection,monkeypatch,tmp_path,request):
    from app.ai.gateway import FunctionRef,ProviderSpec
    from app.core import clock
    from app.routers import session,demo
    from app.services import focus,material_analysis,material_storage,onboarding_dialogue
    instant=[datetime(2026,11,15,23,tzinfo=timezone.utc)]  # 08:00 Monday in Seoul.
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return instant[0].astimezone(tz) if tz else instant[0].replace(tzinfo=None)
    monkeypatch.setattr(clock,'datetime',Clock)
    monkeypatch.setattr(focus,'now',lambda:instant[0])
    is_demo=request.param=='empty_demo'
    monkeypatch.setattr(session,'DEMO_MODE',is_demo)
    monkeypatch.setattr(demo,'DEMO_MODE',is_demo)
    monkeypatch.setattr(material_storage,'STORAGE_DIR',tmp_path/'uploads')
    monkeypatch.setattr(onboarding_dialogue,'PROVIDER',ProviderSpec(FunctionRef('tests.e2e.journey_provider_transport','onboarding')))
    monkeypatch.setattr(material_analysis,'PROVIDER',ProviderSpec(FunctionRef('tests.e2e.material_provider_transport','provider_with_test_transport')))
    monkeypatch.delenv('GROQ_API_KEY',raising=False);monkeypatch.delenv('UPSTAGE_API_KEY',raising=False)
    monkeypatch.delenv('JOURNEY_ONBOARDING_FAIL',raising=False)
    user=data(api_client.post('/api/session'))['anonymousId']
    other=data(api_client.post('/api/session'))['anonymousId']
    day=date(2026,11,16)
    if is_demo:
        seed=data(api_client.post('/api/demo/seed',headers={'X-Anonymous-Id':user},json={'seed':'empty'}))
        day=date.fromisoformat(seed['today'])
    return {'user':user,'headers':{'X-Anonymous-Id':user},'foreign':{'X-Anonymous-Id':other},
        'patch':monkeypatch,'instant':instant,'root':tmp_path/'uploads','day':day,'demo':is_demo}


def pdf(label):
    with pymupdf.open() as document:
        for number in range(8):
            page=document.new_page()
            for line in range(2):page.insert_text((20,30+line*20),f'{label} page {number+1}: Learning material evidence for core ideas.')
        return document.tobytes()


def prepare(client,conn,j):
    headers=j['headers']
    assert data(client.get('/api/subjects',headers=headers))['subjects']==[]
    sid=data(client.post('/api/subjects',headers=headers,json={'name':'자료구조'}))['subjectId'];j['subject']=sid
    text=f"시험은 {(j['day']+timedelta(days=3)).isoformat()}, 목표 A+, 총 목표 없음, 기타 없음"
    key=str(uuid4());body={'text':text,'subjectId':sid}
    j['patch'].setenv('JOURNEY_ONBOARDING_FAIL','1')
    failed=client.post('/api/onboarding/message',headers={**headers,'Idempotency-Key':key},json=body)
    assert failed.status_code==503 and failed.json()['error']['code']=='AI_FAILED'
    assert conn.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id==sid)) is None
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages))==0
    conn.commit()
    j['patch'].delenv('JOURNEY_ONBOARDING_FAIL')
    response=client.post('/api/onboarding/message',headers={**headers,'Idempotency-Key':key},json=body)
    assert data(response)['done']
    assert client.post('/api/onboarding/message',headers={**headers,'Idempotency-Key':key},json=body).json()==response.json()
    assert client.get('/api/onboarding',headers=j['foreign'],params={'subjectId':sid}).status_code==404
    from app.db.session import engine
    from app.services.material_analysis import MaterialAnalysisService
    from tests.test_question_bank_preparation import TransportGateway
    worker=MaterialAnalysisService(engine,storage_dir=j['root'],gateway=TransportGateway(engine))
    mids=[]
    for label in ('first','second'):
        raw=pdf(label)
        def upload(headers=headers):return client.post('/api/materials',headers=headers,data={'subjectId':sid},files={'file':(label+'.pdf',raw,'application/pdf')})
        accepted=data(upload());mids.append(accepted['materialId'])
        assert data(upload())==accepted
        assert upload(j['foreign']).status_code==404
        assert worker.run_job(j['user'],accepted['jobId'])
        finished=data(client.get('/api/materials/jobs/'+accepted['jobId'],headers=headers))
        assert finished['status']=='done',finished
        assert client.get('/api/materials/'+accepted['materialId']+'/file',headers=headers).content==raw
    for _ in range(6):assert worker.run_next()
    assert not worker.run_next()
    proposal=data(client.get('/api/plan/draft',headers=headers))
    assert proposal['summary']['totalPages']==16
    assert client.post('/api/plan/approve',headers=j['foreign'],json={'previewId':proposal['previewId']}).status_code==404
    accepted=client.post('/api/plan/approve',headers=headers,json={'previewId':proposal['previewId']})
    assert data(accepted)=={'approved':True}
    assert client.post('/api/plan/approve',headers=headers,json={'previewId':proposal['previewId']}).json()==accepted.json()
    assert (conn.scalar(select(t.users.c.demo_date).where(t.users.c.id==j['user'])) is not None)==j['demo']
    assert len(list(conn.scalars(select(t.materials.c.id).where(t.materials.c.subject_id==sid))))==2
    conn.commit()
    calendar=data(client.get('/api/calendar',headers=headers,params={'from':j['day'].isoformat(),'to':(j['day']+timedelta(days=3)).isoformat()}))
    assert calendar['study'],calendar
    first=min(row['date'] for row in calendar['study'])
    if j['demo']:
        while j['day']<date.fromisoformat(first):
            j['day']=date.fromisoformat(data(client.post('/api/demo/next-day',headers={**headers,'Idempotency-Key':str(uuid4())}))['today'])
    j['day']=date.fromisoformat(first)
    j['instant'][0]=datetime.combine(j['day'],datetime.min.time(),tzinfo=timezone.utc)-timedelta(hours=1)
    return data(client.get('/api/today',headers=headers))


def learn_today(client,conn,j,today):
    assert today['tasks'],today
    for task in today['tasks']:
        started=data(client.post('/api/study/sessions',headers=j['headers'],json={'taskId':task['id']}))
        session=started['id']
        assert data(client.get('/api/study/sessions/active',headers=j['headers']))['id']==session
        assert client.post('/api/study/sessions/'+session+'/end',headers=j['foreign']).status_code==404
        j['instant'][0]+=timedelta(minutes=10)
        ended=client.post('/api/study/sessions/'+session+'/end',headers=j['headers'])
        assert data(ended)['status']=='ended'
        assert client.post('/api/study/sessions/'+session+'/end',headers=j['headers']).json()==ended.json()
        body={'taskId':task['id'],'sessionId':session,'result':'done'};headers={**j['headers'],'Idempotency-Key':str(uuid4())}
        recorded=client.post('/api/records',headers=headers,json=body);data(recorded)
        assert client.post('/api/records',headers=headers,json=body).json()==recorded.json()
        assert client.post('/api/records',headers=j['foreign'],json=body).status_code==404
    fresh=data(client.get('/api/today',headers=j['headers']))
    assert fresh['progress']['rate']==100
    return fresh


def test_empty_ordinary_account_full_http_journey(api_client,domain_connection,journey):
    client,conn,j=api_client,domain_connection,journey
    today=prepare(client,conn,j);learn_today(client,conn,j,today)
    quiz=data(client.get('/api/quiz/'+j['subject'],headers=j['headers']))
    assert quiz['questions']
    for question in quiz['questions']:
        payload={'choice':0} if question['options'] else {'text':''}
        data(client.put(f"/api/quiz/{quiz['quizId']}/answers/{question['id']}",headers=j['headers'],json={**payload,'confidence':'unsure','revision':0}))
    restored=data(client.get('/api/quiz/'+quiz['quizId'],headers=j['headers']))
    assert len(restored['savedAnswers'])==len(quiz['questions'])
    body={'quizId':quiz['quizId'],'answers':[{k:v for k,v in answer.items() if k!='revision' and v is not None} for answer in restored['savedAnswers']]}
    submitted=client.post('/api/quiz/'+quiz['quizId']+'/submit',headers=j['headers'],json=body);data(submitted)
    assert client.post('/api/quiz/'+quiz['quizId']+'/submit',headers=j['headers'],json=body).json()==submitted.json()
    assert client.get('/api/quiz/'+quiz['quizId'],headers=j['foreign']).status_code==404
    books=data(client.get('/api/books',headers=j['headers']))
    assert books['subjects']
    month=data(client.get('/api/monthly?month=2026-11',headers=j['headers']))
    day=next(row for row in month['days'] if row['date']==j['day'].isoformat())
    assert day['done']==day['plan'] and day['done']>0
    snapshots=list(conn.execute(select(t.study_records)).all());conn.commit()
    j['instant'][0]=datetime.combine(j['day']+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc)-timedelta(hours=9)  # Ordinary local midnight, no demo mutation.
    if j['demo']:
        data(client.post('/api/demo/next-day',headers={**j['headers'],'Idempotency-Key':str(uuid4())}))
    data(client.get('/api/books',headers=j['headers']))
    assert conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==j['user']))==j['day']
    totals=list(conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==j['user'],t.daily_plan_totals.c.study_date==j['day'])).mappings())
    assert totals and all(row['finalized_at'] is not None for row in totals)
    assert conn.execute(select(t.study_records)).all()==snapshots
    conn.commit()
    after=data(client.get('/api/monthly?month=2026-11',headers=j['headers']))
    assert next(row for row in after['days'] if row['date']==j['day'].isoformat())['done']==day['done']
    if not j['demo']:assert client.post('/api/demo/reset',headers=j['headers']).status_code==403
