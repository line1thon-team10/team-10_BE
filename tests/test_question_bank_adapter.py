from copy import deepcopy
from uuid import uuid4

import pytest

from app.ai.gateway import InvalidOutput
from app.ai.question_bank_adapter import DIFFICULTIES, QUESTION_TYPES, validate_question_bank


def context_fixture(difficulty='low'):
    return {'difficulty': difficulty, 'unit': {'title': '자료', 'concepts': [
        {'id': str(uuid4()), 'title': '개념', 'start_page': 1, 'end_page': 1}]},
        'pages': [{'page': 1, 'text': 'A grounded answer from the uploaded material.'},
                  {'page': 2, 'text': 'Another concept outside the question concept.'}]}


def bank_fixture(context):
    return {'questions': [{'id': index, 'type': kind, 'difficulty': DIFFICULTIES[context['difficulty']],
        'question': f'자료 근거 질문 {index}', 'answer': '정답', 'explanation': '원문에 있는 설명',
        'options': ['정답', '오답1', '오답2', '오답3'] if kind == 'multiple_choice' else [],
        'concept_id': context['unit']['concepts'][0]['id'], 'evidence_id': 1, 'keywords': ['정답']}
        for index, kind in enumerate(QUESTION_TYPES, 1)]}


@pytest.mark.parametrize('difficulty', ['low', 'mid', 'high'])
def test_bank_preserves_requested_difficulty_and_exact_ten_grounded_questions(difficulty):
    context = context_fixture(difficulty)
    value = bank_fixture(context)
    assert validate_question_bank(value, context) == value


@pytest.mark.parametrize('invalid', ['short_bank', 'duplicate', 'foreign_concept', 'unknown_evidence',
    'ambiguous_answer', 'missing_explanation', 'wrong_difficulty', 'wrong_type', 'boolean_id', 'null_options'])
def test_invalid_bank_never_passes_the_provider_boundary(invalid):
    context = context_fixture()
    value = deepcopy(bank_fixture(context)); question = value['questions'][0]
    if invalid == 'short_bank': value['questions'].pop()
    if invalid == 'duplicate': value['questions'][1]['question'] = question['question']
    if invalid == 'foreign_concept': question['concept_id'] = str(uuid4())
    if invalid == 'unknown_evidence': question['evidence_id'] = 999
    if invalid == 'ambiguous_answer': question['options'][1] = '정답'
    if invalid == 'missing_explanation': question['explanation'] = ''
    if invalid == 'wrong_difficulty': question['difficulty'] = 'hard'
    if invalid == 'wrong_type': question['type'] = 'essay'
    if invalid == 'boolean_id': question['id'] = True
    if invalid == 'null_options': question['options'] = None
    with pytest.raises(InvalidOutput): validate_question_bank(value, context)
