"""Only the external Groq transport is replaced; adapter/gateway remain real."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from app.ai import resplit_adapter


def resplit_transport_provider(call, context):
    payload = call.input
    behavior = payload.get('_fixtureBehavior', 'valid')
    class GroqTransport:
        def __init__(self, **options):
            assert options['max_retries'] == 0
            self.http = options['http_client']
            self.chat = SimpleNamespace(completions=self)
        def create(self, **options):
            assert options['response_format']['json_schema']['strict'] is True
            assert options['timeout'] > 0
            if behavior == 'failure':
                raise RuntimeError('External transport failed')
            windows = resplit_adapter.explicit_intent(payload)
            value = ({'ready': False, 'question': {'text': '과목, 날짜와 하루 상한을 포함해 다시 알려주세요.', 'choices': []}, 'windows': []}
                     if windows is None else {'ready': True, 'question': None, 'windows': windows})
            if behavior == 'fabricated':
                value = {'ready': True, 'question': None, 'windows': [{'subjectId': payload['subjects'][0]['id'],
                         'dates': [payload['today']], 'minutesPerDay': 1}]}
            body = json.dumps(value)
            if behavior == 'syntax_always' or behavior == 'syntax_once' and not call.format_retry:
                body = '{'
            return SimpleNamespace(id='fixture-groq-request', usage=SimpleNamespace(prompt_tokens=13, completion_tokens=7),
                                   choices=[SimpleNamespace(message=SimpleNamespace(content=body))])
        def close(self):
            self.http.close()
    with patch.dict(os.environ, {'GROQ_API_KEY': 'local-transport-fixture'}), patch('groq.Groq', GroqTransport):
        return resplit_adapter.resplit_provider(call, context)
