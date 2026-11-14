"""Only replace the paid SDK call; use the real prompt, gateway and validator."""
import json
from types import SimpleNamespace


def provider(call, context):
    import groq
    from app.ai.record_interpret_adapter import record_provider
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
            self.http = kwargs['http_client']
            self.chat = SimpleNamespace(completions=self)
        def create(self, **kwargs):
            assert 0 < kwargs['timeout'] <= 25
            source = json.loads(kwargs['messages'][1]['content'])
            assert source['text'] == '13쪽까지 읽었어'
            assert len(source['ranges']) == 1 and source['ranges'][0]['from'] == 10
            value = {'isConfirmed': True, 'completedRanges': [{'rangeId': source['ranges'][0]['id'], 'toPage': 13}]}
            return SimpleNamespace(id='independent-record', usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10),
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])
        def close(self): self.http.close()
    original = groq.Groq
    groq.Groq = Client
    try: return record_provider(call, context)
    finally: groq.Groq = original
