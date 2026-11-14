"""Process-importable transport fixture; real provider/gateway validation remains."""
import json
import os
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch


def onboarding(call, context):
    from app.ai.onboarding_adapter import onboarding_provider
    from app.domain.onboarding import GOAL_COLUMNS
    payload=call.input
    exam=(date.fromisoformat(payload['today'])+timedelta(days=3)).isoformat()
    values=dict.fromkeys(GOAL_COLUMNS)
    values.update(exam_date=exam,target_grade='A+',target_score=90)
    value={'message':'시험과 목표를 확인했어요','chips':[],'isConfirmed':True,'event':None,
        'goals':[{'subjectId':payload['subjects'][0]['id'],'values':values,
            'providedFields':['exam_date','target_grade','target_score'],
            'confirmedFields':dict.fromkeys(('examDate','goal','goalTotal','goalEtc'),True),
            'confirmationEvidence':{'examDate':f'시험은 {exam}','goal':'목표 A+',
                'goalTotal':'총 목표 없음','goalEtc':'기타 없음'}}]}
    class Client:
        def __init__(self,**kwargs):
            self.http=kwargs['http_client'];self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def create(self,**kwargs):
            if os.environ.get('JOURNEY_ONBOARDING_FAIL')=='1':raise RuntimeError('external transport unavailable')
            return SimpleNamespace(id='journey-onboarding',choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))],
                usage=SimpleNamespace(prompt_tokens=10,completion_tokens=10))
        def close(self):self.http.close()
    with patch.dict(os.environ,{'GROQ_API_KEY':'test-no-network'}),patch('groq.Groq',Client):
        return onboarding_provider(call,context)
