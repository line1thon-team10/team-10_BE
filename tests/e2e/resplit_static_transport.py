"""Independent model output: never derive the oracle using the production parser."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from app.ai import resplit_adapter


def provider(call, context):
    outputs = json.loads(os.environ['INDEPENDENT_RESPLIT_OUTPUTS'])
    value = outputs[1 if call.input.get('answer') is not None else 0]

    class Transport:
        def __init__(self, **options):
            self.http = options['http_client']
            self.chat = SimpleNamespace(completions=self)

        def create(self, **options):
            return SimpleNamespace(id='independent-resplit',
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=9),
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])

        def close(self):
            self.http.close()

    with patch('groq.Groq', Transport):
        return resplit_adapter.resplit_provider(call, context)
