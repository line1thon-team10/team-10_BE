"""Prepare a bounded bank from the analyzed PDF text, without file caches."""
import json

from app.ai import engine
from app.ai.gateway import FunctionRef, InvalidOutput, ProviderSpec
from app.ai.material_adapter import MODEL_VERSION, ObservedGroq

PROMPT_VERSION = 'question-bank-6d74ffb6-adapter-v1'
SCHEMA_VERSION = 'question-bank-v1'
PROVIDER = ProviderSpec(FunctionRef(__name__, 'question_bank_provider'))
VALIDATOR = FunctionRef(__name__, 'validate_question_bank')
DIFFICULTIES = {'low': 'easy', 'mid': 'medium', 'high': 'hard'}
QUESTION_TYPES = ['multiple_choice'] * 6 + ['short_answer'] * 3 + ['essay']


def evidence_for(context):
    bank = engine.module('quiz_bank')
    pages = {page for concept in context['unit']['concepts']
             for page in range(concept['start_page'], concept['end_page'] + 1)}
    evidence = bank.build_evidence(context['pages'], pages)
    if len(evidence) > 24:
        evidence = evidence[::max(1, (len(evidence) + 23) // 24)][:24]
    return evidence


def validate_question_bank(value, context):
    if type(value) is not dict or set(value) != {'questions'} or type(value['questions']) is not list:
        raise InvalidOutput('Invalid question bank')
    if len(value['questions']) != len(QUESTION_TYPES):
        raise InvalidOutput('A bank must contain exactly ten questions')
    bank = engine.module('quiz_bank')
    evidence = {item['id']: item for item in evidence_for(context)}
    concepts = {item['id']: {'id': item['id'], 'page_start': item['start_page'], 'page_end': item['end_page']}
                for item in context['unit']['concepts']}
    seen = set()
    expected_keys = set(bank.BANK_SCHEMA['properties']['questions']['items']['required'])
    for number, (question, kind) in enumerate(zip(value['questions'], QUESTION_TYPES), 1):
        if (type(question) is not dict or set(question) != expected_keys
                or type(question['id']) is not int or type(question['evidence_id']) is not int
                or question['difficulty'] != DIFFICULTIES[context['difficulty']]
                or any(type(question[key]) is not str or not question[key].strip()
                       for key in ('question', 'answer', 'explanation', 'concept_id'))
                or any(type(question[key]) is not list or any(type(item) is not str for item in question[key])
                       for key in ('options', 'keywords'))):
            raise InvalidOutput('Invalid question fields')
        try:
            # The pinned validator implements exact count/type, unique prompts,
            # concept/evidence page binding, and unambiguous choice answers.
            # Its bank is easy-only; the requested difficulty is checked above.
            bank._validate_question({**question, 'difficulty': 'easy'}, number, kind, evidence, concepts, seen)
        except (ValueError, TypeError, KeyError) as error:
            raise InvalidOutput('Question is not grounded in its concept') from error
    return value


def question_bank_provider(call, context):
    payload = call.input
    bank = engine.module('quiz_bank')
    evidence = evidence_for(payload)
    if not evidence:
        raise InvalidOutput('No extractable concept evidence')
    request = {'unit': payload['unit']['title'], 'difficulty': DIFFICULTIES[payload['difficulty']],
        'concepts': payload['unit']['concepts'], 'evidence': evidence,
        'plan': [{'id': index, 'type': kind, 'difficulty': DIFFICULTIES[payload['difficulty']]}
                 for index, kind in enumerate(QUESTION_TYPES, 1)]}
    messages = [{'role': 'system', 'content':
        '제공된 원문 근거만 사용해 한국어 퀴즈 10개를 생성하세요. plan의 번호·유형·난이도를 정확히 따르세요. '
        '각 문항은 해당 개념 start_page~end_page 안의 evidence_id와 concept_id를 사용하고 '
        '그 원문이 정답을 직접 뒷받침해야 합니다. 질문은 중복하지 말고 정답과 해설을 모두 포함하세요. '
        '객관식 options는 서로 다른 네 보기이며 answer는 보기 하나와 정확히 같아야 합니다. '
        '단답·서술 options는 빈 배열이며 keywords도 문자열 배열입니다. JSON만 반환하세요.'},
        {'role': 'user', 'content': json.dumps(request, ensure_ascii=False)}]
    groq = ObservedGroq(context, call.format_retry)
    try:
        return groq.generate(messages, bank.BANK_SCHEMA, 'question_bank', max_tokens=6000)
    except json.JSONDecodeError as error:
        return error.doc
    finally:
        groq.close()
