"""Replace only Groq's external transport in the real bank provider process."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch


def provider_with_test_transport(call, context):
    from app.ai.question_bank_adapter import question_bank_provider

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
            self.http = kwargs['http_client']
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            mode = os.environ.get('BANK_E2E_TRANSPORT', 'valid')
            if mode == 'failed':
                raise RuntimeError('simulated question provider failure')
            request = json.loads(kwargs['messages'][1]['content'])
            evidence = request['evidence'][0]
            concept = next(item for item in request['concepts']
                           if item['start_page'] <= evidence['page'] <= item['end_page'])
            questions = [{**plan, 'question': f"{request['difficulty']} 원문 질문 {plan['id']}",
                'answer': '정답', 'explanation': '원문을 바탕으로 한 해설', 'evidence_id': evidence['id'],
                'concept_id': concept['id'], 'keywords': ['정답'],
                'options': ['정답', '오답1', '오답2', '오답3'] if plan['type'] == 'multiple_choice' else []}
                for plan in request['plan']]
            if mode == 'invalid':
                questions[-1]['evidence_id'] = 999
            content = json.dumps({'questions': questions})
            return SimpleNamespace(id='test-bank', choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                usage=SimpleNamespace(prompt_tokens=20, completion_tokens=60))

        def close(self):
            self.http.close()

    with patch.dict(os.environ, {'GROQ_API_KEY': 'test-no-network'}), patch('groq.Groq', Client):
        return question_bank_provider(call, context)
