"""Pinned tutor prompts with exact owned source mapping and unsanitized citations."""
import hashlib
import json
from pathlib import Path
import re

from app.ai import engine
from app.ai.gateway import FunctionRef, InvalidOutput, ProviderSpec
from app.ai.material_adapter import MODEL_VERSION, ObservedGroq

PROMPT_VERSION = 'tutor-6d74ffb6-grounded-v1'
SCHEMA_VERSION = 'tutor-grounded-v1'
PROVIDER = ProviderSpec(FunctionRef(__name__, 'tutor_provider'))
VALIDATOR = FunctionRef(__name__, 'validate_tutor_result')


def read_sources(materials):
    sources = []
    for material in materials:
        pages = material.get('pages')
        if pages is None:
            path = Path(material['path']) if material.get('path') else None
            if path is None or path.resolve() != path or not path.is_file():
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != material['sha256']:
                raise InvalidOutput('PDF content changed')
            import pymupdf
            with pymupdf.open(path) as document:
                if document.page_count != material['pageCount']:
                    raise InvalidOutput('PDF page count changed')
                pages = [{'page': index + 1, 'text': page.get_text()} for index, page in enumerate(document)]
        seen = set()
        for page in pages:
            number, text = page.get('page'), page.get('text')
            if type(number) is not int or not 1 <= number <= material['pageCount'] or number in seen or type(text) is not str:
                raise InvalidOutput('Invalid persisted page evidence')
            seen.add(number)
            if text.strip():
                sources.append({'materialId': material['id'], 'subjectId': material['subjectId'],
                    'subjectName': material['subjectName'], 'page': number, 'text': text.strip()[:9000]})
    return sources


def selected_sources(payload):
    sources = read_sources(payload['materials'])
    parent = payload.get('parent')
    if parent is not None:
        allowed = {(item['materialId'], page) for item in parent['pages'] for page in range(item['from'], item['to'] + 1)}
        sources = [source for source in sources if (source['materialId'], source['page']) in allowed
                   and source['subjectId'] == parent['subjectId']]
        if {(source['materialId'], source['page']) for source in sources} != allowed:
            raise InvalidOutput('Original evidence is unavailable')
        current = {row['id']: row for row in payload['materials']}
        for identifier, version in (parent.get('versions') or {}).items():
            if identifier not in current or [current[identifier]['sha256'], current[identifier]['analysisVersion']] != version:
                raise InvalidOutput('Original material version changed')
        for cite in parent.get('sources', []):
            actual = '\n'.join(source['text'] for source in sources if source['materialId'] == cite['materialId']
                               and cite['from'] <= source['page'] <= cite['to'])
            if not cite.get('quote') or cite['quote'] not in actual:
                raise InvalidOutput('Original quotation is no longer available')
        return sources
    terms = set(re.findall(r'[가-힣A-Za-z0-9]{2,}', payload['question'].lower()))
    def score(source):
        return sum(source['text'].lower().count(term) for term in terms) + (
            100 if source['subjectName'].lower() in payload['question'].lower() else 0)
    subjects = {}
    for source in sources:
        subjects[source['subjectId']] = max(subjects.get(source['subjectId'], -1), score(source))
    if not subjects:
        raise InvalidOutput('No material evidence')
    best = max(subjects.values())
    winners = [key for key, value in subjects.items() if value == best]
    if len(winners) != 1:
        raise InvalidOutput('The target subject is ambiguous')
    return sorted((source for source in sources if source['subjectId'] == winners[0]),
                  key=lambda source: (-score(source), source['materialId'], source['page']))[:8]


def validate_tutor_result(value, context):
    if type(value) is not dict or set(value) != {'subjectId', 'answer', 'sources'}:
        raise InvalidOutput('Invalid tutor answer shape')
    if type(value['answer']) is not str or not value['answer'].strip():
        raise InvalidOutput('An answer is required')
    if type(value['sources']) is not list or not value['sources']:
        raise InvalidOutput('A grounded answer requires cited pages')
    selected = selected_sources(context)
    allowed = {(source['materialId'], source['page']): source for source in selected}
    seen = set()
    for cite in value['sources']:
        if type(cite) is not dict or set(cite) != {'materialId', 'from', 'to', 'quote'}:
            raise InvalidOutput('Invalid citation shape')
        if type(cite['from']) is not int or type(cite['to']) is not int or cite['from'] != cite['to']:
            raise InvalidOutput('Citations must identify exact physical pages')
        key = (cite['materialId'], cite['from'])
        source = allowed.get(key)
        if (source is None or key in seen or source['subjectId'] != value['subjectId']
                or type(cite['quote']) is not str or not cite['quote'].strip() or cite['quote'] not in source['text']):
            raise InvalidOutput('Citation is not grounded in the selected owned source')
        seen.add(key)
    return value


class CapturedResponse(Exception):
    def __init__(self, value):
        self.value = value


class CaptureGroq:
    def __init__(self, delegate):
        self.delegate = delegate

    def generate(self, *args, **kwargs):
        # The pinned helper silently filters unknown page IDs and forces parent
        # pages on simplify. Validate the actual transport output before that.
        raise CapturedResponse(self.delegate.generate(*args, **kwargs))


def tutor_provider(call, context):
    payload = call.input
    selected = selected_sources(payload)
    if not selected:
        raise InvalidOutput('No usable evidence')
    numbered = [{'page': index, 'text': source['text']} for index, source in enumerate(selected, 1)]
    groq = ObservedGroq(context, call.format_retry)
    try:
        try:
            pinned = engine.module('ai_tutor')
            if payload.get('parent') is None:
                pinned.answer_tutor(payload['question'], [], numbered, groq=CaptureGroq(groq))
            else:
                parent = payload['parent']
                pinned.simplify_tutor(parent['question'], parent['answer'], list(range(1, len(numbered) + 1)),
                                      numbered, groq=CaptureGroq(groq))
        except CapturedResponse as captured:
            raw = captured.value
        else:
            raise InvalidOutput('The tutor did not use the observed transport')
    except json.JSONDecodeError as error:
        return error.doc
    finally:
        groq.close()
    if type(raw) is not dict or set(raw) != {'answer', 'pages'} or type(raw['pages']) is not list or not raw['pages']:
        raise InvalidOutput('The tutor returned no grounded references')
    cites = []
    for page in raw['pages']:
        if type(page) is not int or not 1 <= page <= len(selected):
            raise InvalidOutput('The tutor cited an unknown page')
        source = selected[page - 1]
        cites.append({'materialId': source['materialId'], 'from': source['page'], 'to': source['page'], 'quote': source['text']})
    return {'subjectId': selected[0]['subjectId'], 'answer': raw['answer'], 'sources': cites}
