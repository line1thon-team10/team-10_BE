"""Process-importable TOC transport double; never calls a remote provider."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch


def toc_with_test_transport(call, context):
    from app.services.material_toc import toc_provider
    mode = os.environ.get('MATERIAL_E2E_TOC', 'good')

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
            self.http = kwargs['http_client']
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            if mode == 'failed':
                raise RuntimeError('simulated TOC transport failure')
            assert kwargs['response_format']['json_schema']['name'] == 'manual_contents'
            units = [{'no': 1, 'title': 'Intro', 'from': 1, 'to': 1},
                     {'no': 2, 'title': 'Details', 'from': 2, 'to': 2}]
            if mode == 'ambiguous':
                units = []
            if mode == 'outside':
                units[1]['to'] = 3
            return SimpleNamespace(id='test-toc', choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({'units': units})))], usage=SimpleNamespace(prompt_tokens=13, completion_tokens=7))

        def close(self):
            self.http.close()

    with patch.dict(os.environ, {'GROQ_API_KEY': 'test-no-network'}), patch('groq.Groq', Client):
        return toc_provider(call, context)
