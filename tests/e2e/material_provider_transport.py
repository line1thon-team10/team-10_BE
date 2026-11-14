"""Process-importable external transport doubles for independent material E2E."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch


def provider_with_test_transport(call, context):
    import httpx
    from app.ai.material_adapter import material_provider

    mode = os.environ.get('MATERIAL_E2E_TRANSPORT', 'native')
    total = call.input['totalPages']

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
            assert 0 < kwargs['timeout'] <= context.remaining_seconds + 1
            self.http = kwargs['http_client']
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            assert kwargs['timeout'] > 0
            if mode == 'provider_failed':
                raise RuntimeError('simulated external transport failure')
            if mode == 'provider_timeout':
                raise TimeoutError('simulated external timeout')
            name = kwargs['response_format']['json_schema']['name']
            values = {
                'document_metadata': {'document_title': 'Learning notes', 'section_titles': ['Core ideas']},
                'outline_chunk': {'units': [{'title': 'Core ideas', 'parent_section': 'Core ideas',
                    'start_page': 1, 'end_page': total + (1 if mode == 'bad_pages' else 0)}],
                    'excluded_pages': []},
                'unit_content': {'key_concepts': [{'name': 'Important concept', 'pages': list(range(1, total + 1))}],
                    'summary': 'A summary supported by the test document.'},
            }
            content = json.dumps(values[name])
            if mode == 'malformed_json':
                content = '{not valid JSON'
            return SimpleNamespace(id=f'test-{name}', choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                usage=SimpleNamespace(prompt_tokens=17, completion_tokens=9))

        def close(self):
            self.http.close()

    def post(_client, url, **kwargs):
        assert url == 'https://api.upstage.ai/v1/document-digitization'
        assert kwargs['timeout'] > 0
        if mode == 'ocr_failed':
            raise httpx.ConnectError('simulated OCR transport failure')
        text = '' if mode == 'ocr_low_quality' else 'Readable source document content ' * 5
        payload = {'elements': [{'page': page, 'content': {'markdown': text}}
                                for page in range(1, total + 1)]}
        return httpx.Response(200, json=payload, headers={'x-request-id': 'test-ocr'},
            request=httpx.Request('POST', url))

    with patch.dict(os.environ, {'GROQ_API_KEY': 'test-no-network', 'UPSTAGE_API_KEY': 'test-no-network'}), \
            patch('groq.Groq', Client), patch('httpx.Client.post', post):
        return material_provider(call, context)

