"""Parse manual contents without changing persisted materials or structures."""
import re

from app.ai.gateway import (AiGateway, FunctionRef, GatewayRequest, InvalidOutput,
                            MaterialRevision, ProviderSpec, ResultIdentity)
from app.ai.material_adapter import MODEL_VERSION, ObservedGroq
from app.core.responses import ApiError
from app.repositories.materials import MaterialRepository

LINE = re.compile(r'^\s*(\d+)[.)]\s*(.+?)\s+(?:p(?:age)?\.?\s*)?(\d+)\s*[~\-–]\s*(\d+)\s*(?:쪽|p)?\s*$', re.IGNORECASE)
VALIDATOR = FunctionRef(__name__, 'validate_toc')
PROVIDER = ProviderSpec(FunctionRef(__name__, 'toc_provider'))


def validate_toc(value, context):
    if type(value) is not dict or set(value) != {'units'} or type(value['units']) is not list or not value['units']:
        raise InvalidOutput('Manual contents require nonempty units')
    count, numbers, intervals = context['pageCount'], set(), []
    for unit in value['units']:
        if type(unit) is not dict or set(unit) != {'no', 'title', 'from', 'to'}:
            raise InvalidOutput('Invalid contents fields')
        number, start, end = unit['no'], unit['from'], unit['to']
        if (any(type(item) is not int for item in (number, start, end)) or number < 1 or number in numbers
                or not 1 <= start <= end <= count):
            raise InvalidOutput('Invalid contents number or physical page boundary')
        if type(unit['title']) is not str or not unit['title'].strip():
            raise InvalidOutput('Contents title is missing')
        numbers.add(number)
        intervals.append((start, end))
    intervals.sort()
    if any(right[0] <= left[1] for left, right in zip(intervals, intervals[1:])):
        raise InvalidOutput('Contents pages overlap')
    return value


def toc_provider(call, context):
    import json
    groq = ObservedGroq(context, call.format_retry)
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['units'], 'properties': {
        'units': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['no', 'title', 'from', 'to'], 'properties': {
                'no': {'type': 'integer'}, 'title': {'type': 'string'},
                'from': {'type': 'integer'}, 'to': {'type': 'integer'}}}}}}
    try:
        return groq.generate([
            {'role': 'system', 'content': '사용자가 적은 목차의 단원 번호, 제목, 실제 PDF 페이지 범위만 해석하세요. '
             '제공되지 않은 제목이나 페이지를 추측하지 마세요. 해석할 수 없으면 units를 빈 배열로 반환하세요.'},
            {'role': 'user', 'content': json.dumps(call.input, ensure_ascii=False)}], schema, 'manual_contents', 1800)
    except json.JSONDecodeError as error:
        return error.doc
    finally:
        groq.close()


class MaterialTocService:
    def __init__(self, engine, *, gateway=None):
        self.engine = engine
        self.gateway = gateway or AiGateway(engine)

    def parse(self, user_id, subject_id, material_id, text):
        with self.engine.connect() as conn:
            material = MaterialRepository(conn).material(user_id, material_id)
        if material['subject_id'] != subject_id:
            raise ApiError('INVALID_INPUT', '자료와 과목이 일치하지 않아요', 422)
        pages = material['page_count']
        if type(pages) is not int or pages < 1:
            raise ApiError('INVALID_INPUT', 'PDF 페이지 수를 먼저 확인해 주세요', 422)
        if type(text) is not str or not text.strip():
            raise ApiError('INVALID_INPUT', '목차 내용을 입력해 주세요', 422)
        matches = [LINE.fullmatch(line) for line in text.splitlines() if line.strip()]
        context = {'pageCount': pages}
        if all(matches):
            result = {'units': [{'no': int(item[1]), 'title': item[2].strip(),
                                 'from': int(item[3]), 'to': int(item[4])} for item in matches]}
            try:
                return validate_toc(result, context)
            except InvalidOutput:
                raise ApiError('INVALID_INPUT', '단원 번호와 PDF 페이지 범위를 확인해 주세요', 422) from None
        identity = ResultIdentity.create(kind='nl_parse', provider='groq', model_version=MODEL_VERSION,
            prompt_version='manual-contents-v1', schema_version='manual-contents-v1',
            input={'text': text, 'physicalPages': pages}, validation_context=context,
            materials=(MaterialRevision(material_id, material['sha256'] + ':' + material['analysis_version']),))
        outcome = self.gateway.run(GatewayRequest(user_id, identity), validator=VALIDATOR, provider=PROVIDER)
        return validate_toc(outcome.data, context)
