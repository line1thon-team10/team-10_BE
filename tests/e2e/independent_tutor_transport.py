"""Record the real pinned prompt; replace only the paid Groq HTTP boundary."""
import json
import os
from pathlib import Path
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
            prompt = json.loads(kwargs['messages'][1]['content'])
            Path(os.environ['INDEPENDENT_TUTOR_PROMPT']).write_text(json.dumps(prompt), encoding='utf-8')
            value = {'answer': prompt['source_pages'][0]['text'], 'pages': [1]}
            return SimpleNamespace(id='independent-tutor', usage=SimpleNamespace(prompt_tokens=35, completion_tokens=12),
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])
        def close(self):
            self.http.close()
    original = groq.Groq
    groq.Groq = Client
    try:
        return tutor_provider(call, context)
    finally:
        groq.Groq = original
