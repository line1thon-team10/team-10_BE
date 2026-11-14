"""Only the paid Groq transport is replaced; pinned prompts and gateway run."""
import json
import os
from types import SimpleNamespace


def provider(call, context):
    import groq
    from app.ai.tutor_adapter import tutor_provider

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
            self.http = kwargs['http_client']
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            assert 0 < kwargs['timeout'] <= 25
            mode = os.environ.get('TUTOR_TEST_MODE', 'valid')
            if mode == 'fail':
                raise RuntimeError('fixture upstream failure')
            value = {'answer': 'AVL rotations preserve the search tree ordering.', 'pages': [1]}
            if '쉬운 말' in kwargs['messages'][0]['content']:
                value['answer'] = 'A rotation rearranges tree nodes while keeping their order.'
            if mode == 'no_cites': value['pages'] = []
            if mode == 'outside': value['pages'] = [999]
            if mode == 'boolean': value['pages'] = [True]
            if mode == 'duplicate': value['pages'] = [1, 1]
            if mode == 'extra': value['invented'] = True
            if mode == 'empty': value['answer'] = ''
            raw = '{not json' if mode == 'json' else json.dumps(value)
            return SimpleNamespace(id='fixture-tutor', usage=SimpleNamespace(prompt_tokens=35, completion_tokens=12),
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])

        def close(self):
            self.http.close()

    original = groq.Groq
    groq.Groq = Client
    try:
        return tutor_provider(call, context)
    finally:
        groq.Groq = original
