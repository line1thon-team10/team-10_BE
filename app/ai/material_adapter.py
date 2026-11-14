"""Bounded PDF extraction and pinned analysis, without shared file caches."""
import hashlib
import importlib
import json
import os
from pathlib import Path

from app.ai import engine
from app.ai.gateway import FunctionRef, InvalidOutput, ProviderSpec, parse_json

MODEL_VERSION = 'openai/gpt-oss-120b'
PROMPT_VERSION = 'material-6d74ffb6-adapter-v1'
SCHEMA_VERSION = 'material-analysis-result-v1'
PROVIDER = ProviderSpec(FunctionRef(__name__, 'material_provider'))
VALIDATOR = FunctionRef(__name__, 'validate_material_result')


def quality_report(pages, total):
    engine._prepare_path()
    quality = importlib.import_module('services.pdf_quality').evaluate_pdf_quality(pages, total)
    return {**quality, 'version': 'text-quality-v1'}


def validate_material_result(value, context):
    if type(value) is not dict or value.get('schemaVersion') != SCHEMA_VERSION:
        raise InvalidOutput('Invalid material result version')
    for name in ('sourceSha256', 'analysisVersion', 'totalPages'):
        if value.get(name) != context[name]:
            raise InvalidOutput('Material result does not match its input')
    total = value['totalPages']
    if type(total) is not int or total < 1 or value.get('method') not in ('native', 'ocr'):
        raise InvalidOutput('Invalid extraction metadata')
    if value.get('quality') != quality_report(value.get('pages'), total):
        raise InvalidOutput('Quality must match the extracted page text')
    units = value.get('units')
    if type(units) is not list:
        raise InvalidOutput('Units must be a list')
    if value['quality']['quiz_allowed'] and not units:
        raise InvalidOutput('Readable material requires a structure')
    previous = 0
    for unit in units:
        start, end = unit.get('start_page'), unit.get('end_page')
        if any(type(x) is not int for x in (start, end)) or not previous < start <= end <= total:
            raise InvalidOutput('Unit pages overlap or exceed physical pages')
        previous = end
        if type(unit.get('title')) is not str or not unit['title'].strip():
            raise InvalidOutput('Unit title is missing')
        if unit.get('difficulty') not in (None, 'low', 'mid', 'high'):
            raise InvalidOutput('Invalid difficulty')
        if type(unit.get('evidence')) is not list or any(type(x) is not str for x in unit['evidence']):
            raise InvalidOutput('Evidence must be text')
        if type(unit.get('concepts')) is not list:
            raise InvalidOutput('Concepts must be a list')
        for concept in unit['concepts']:
            left, right = concept.get('start_page'), concept.get('end_page')
            if any(type(x) is not int for x in (left, right)) or not start <= left <= right <= end:
                raise InvalidOutput('Concept must be inside its unit')
            if type(concept.get('title')) is not str or not concept['title'].strip():
                raise InvalidOutput('Concept title is missing')
    return value


class ObservedGroq:
    """Pinned prompt functions receive this adapter instead of retrying GroqService."""
    def __init__(self, context, format_retry=False):
        self.context, self.format_retry = context, format_retry
        key = os.environ.get('GROQ_API_KEY')
        if not key or not key.strip():
            raise RuntimeError('Analysis provider is not configured')
        import httpx
        from groq import Groq
        transport = httpx.HTTPTransport(retries=0)
        http = httpx.Client(transport=transport, follow_redirects=False, timeout=context.remaining_seconds)
        try:
            self.client = Groq(api_key=key, max_retries=0, timeout=context.remaining_seconds, http_client=http)
        except BaseException:
            http.close()
            raise

    def generate(self, messages, schema, name, max_tokens=1200):
        if self.format_retry:
            messages = [*messages, {'role': 'system', 'content': '올바른 JSON 객체만 반환하세요.'}]
        request_id = self.context.start_request(provider='groq', model=MODEL_VERSION)
        try:
            response = self.client.chat.completions.create(model=MODEL_VERSION, messages=messages,
                temperature=0.1, max_completion_tokens=max_tokens, timeout=self.context.remaining_seconds,
                response_format={'type': 'json_schema', 'json_schema': {
                    'name': name, 'strict': True, 'schema': schema}})
        except BaseException:
            self.context.finish_request(request_id, status='failed')
            raise
        usage = getattr(response, 'usage', None)
        self.context.finish_request(request_id, provider_request_id=getattr(response, 'id', None),
            input_tokens=getattr(usage, 'prompt_tokens', None), output_tokens=getattr(usage, 'completion_tokens', None))
        return parse_json(response.choices[0].message.content)

    def close(self):
        self.client.close()


def _ocr(path, total, context):
    key = os.environ.get('UPSTAGE_API_KEY')
    if not key or not key.strip():
        raise RuntimeError('OCR provider is not configured')
    import httpx
    request_id = context.start_request(provider='upstage', model='document-parse')
    try:
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), follow_redirects=False,
                          timeout=context.remaining_seconds) as client, path.open('rb') as source:
            response = client.post('https://api.upstage.ai/v1/document-digitization',
                headers={'Authorization': 'Bearer ' + key},
                data={'model': 'document-parse', 'ocr': 'force', 'output_formats': '["markdown"]'},
                files={'document': (path.name, source, 'application/pdf')}, timeout=context.remaining_seconds)
            response.raise_for_status()
            value = response.json()
        context.finish_request(request_id, provider_request_id=response.headers.get('x-request-id'))
    except BaseException:
        context.finish_request(request_id, status='failed')
        raise
    elements = value.get('elements')
    if type(elements) is not list or not elements:
        raise InvalidOutput('OCR returned no page elements')
    content = {page: [] for page in range(1, total + 1)}
    for element in elements:
        page = element.get('page')
        if type(page) is not int or page not in content or type(element.get('content')) is not dict:
            raise InvalidOutput('OCR page mapping is invalid')
        text = element['content'].get('markdown', '')
        if type(text) is not str:
            raise InvalidOutput('OCR text is invalid')
        content[page].append(text)
    return [{'page': page, 'text': '\n'.join(text)} for page, text in content.items()]


def material_provider(call, context):
    import pymupdf
    payload = call.input
    path = Path(payload['path'])
    if path.resolve() != path or not path.is_file():
        raise InvalidOutput('Material original is unavailable')
    if hashlib.sha256(path.read_bytes()).hexdigest() != payload['sourceSha256']:
        raise InvalidOutput('Material original changed')
    with pymupdf.open(path) as document:
        total = document.page_count
        pages = [{'page': index + 1, 'text': page.get_text()} for index, page in enumerate(document)]
    if total != payload['totalPages']:
        raise InvalidOutput('Physical PDF page count changed')
    method = 'native'
    quality = quality_report(pages, total)
    if not quality['quiz_allowed']:
        pages = _ocr(path, total, context)
        method, quality = 'ocr', quality_report(pages, total)
    units = []
    if quality['quiz_allowed']:
        engine._prepare_path()
        structure_module = importlib.import_module('services.document_structure')
        validator = importlib.import_module('services.analysis_validator')
        analyzer = importlib.import_module('services.unit_analyzer')
        groq = ObservedGroq(context, call.format_retry)
        try:
            metadata = structure_module.extract_metadata(groq, pages)
            structure = validator.validate_structure(structure_module.build_document_structure(groq, pages, metadata), total)
            for index, unit in enumerate(structure.units, 1):
                analyzed = analyzer.analyze_unit(groq, pages, unit, index)
                units.append({'title': analyzed.title, 'start_page': analyzed.start_page,
                    'end_page': analyzed.end_page, 'difficulty': {'하': 'low', '중': 'mid', '상': 'high'}.get(analyzed.difficulty),
                    'evidence': [f'PDF {analyzed.start_page}~{analyzed.end_page}쪽 본문 분석'],
                    'concepts': [{'title': item['name'], 'start_page': item['page_start'],
                                  'end_page': item['page_end']} for item in analyzed.concepts]})
        except json.JSONDecodeError as error:
            # Gateway owns the single syntax retry, including its remaining budget.
            return error.doc
        finally:
            groq.close()
    return {'schemaVersion': SCHEMA_VERSION, 'sourceSha256': payload['sourceSha256'],
        'analysisVersion': payload['analysisVersion'], 'totalPages': total,
        'method': method, 'quality': quality, 'pages': pages, 'units': units}
