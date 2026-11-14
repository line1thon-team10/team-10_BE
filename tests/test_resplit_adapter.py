"""Strict source semantics for scheduling requests; no external AI calls."""
from types import SimpleNamespace
import json

import pytest

from app.ai import resplit_adapter as adapter
from app.ai.gateway import InvalidOutput

A = '00000000-0000-4000-8000-000000000001'
B = '00000000-0000-4000-8000-000000000002'


def context(text, answer=None):
    return {'request': text, 'answer': answer, 'today': '2026-11-14', 'basisHash': 'basis',
            'subjects': [{'id': A, 'name': '수학', 'examDate': '2026-11-21'},
                         {'id': B, 'name': '자료구조', 'examDate': '2026-11-24'}]}


def window(subject, dates, cap=None):
    return {'subjectId': subject, 'dates': [f'2026-11-{day:02}' for day in dates], 'minutesPerDay': cap}


def ready(windows):
    return {'ready': True, 'question': None, 'windows': windows}


@pytest.mark.parametrize('text,expected', [
    ('수학 월요일 수요일만 하루 60분 이하로 배분해줘', [window(A, [16, 18], 60)]),
    ('자료구조 월수금 하루 1시간 30분으로 해줘', [window(B, [16, 18, 20, 23], 90)]),
    ('전체 과목 하루 45분 이하로 다시 배분해줘', [window(None, range(14, 24), 45)]),
    ('수학 2026-11-16부터 2026-11-18까지 하루 30분', [window(A, [16, 17, 18], 30)]),
    ('수학 내일부터 11월 18일까지 하루 1시간', [window(A, [15, 16, 17, 18], 60)]),
    ('수학 11월 16일, 11월 18일에만 공부해줘', [window(A, [16, 18])]),
    ('수학 평일만 30분씩 배분해줘', [window(A, range(16, 21), 30)]),
    ('자료구조 다음 주 주말만 하루 60분', [window(B, [21, 22], 60)]),
    ('수학 이번 주 주말만 공부해줘', [window(A, [14, 15])]),
    ('수학 2026년 11월 16일 하루 90분', [window(A, [16], 90)]),
    ('수학 내일부터 하루 45분', [window(A, range(15, 21), 45)]),
    ('수학 모레까지 하루 20분', [window(A, [14, 15, 16], 20)]),
    ('수학 월요일 하루 30분 그리고 자료구조 화요일 하루 45분',
        [window(A, [16], 30), window(B, [17], 45)]),
    ('수학과 자료구조 월요일 하루 60분', [window(A, [16], 60), window(B, [16, 23], 60)]),
    ('남은 전체 범위를 다시 배분해줘', []),
    ('다시 나눠줘', []),
    ('내일부터 다시 나눠줘', [window(None, range(15, 24))]),
    ('현재 일정과 목표대로 재생성', []),
])
def test_grounded_windows(text, expected):
    ctx = context(text)
    assert adapter.explicit_intent(ctx) == expected
    assert adapter.validate_resplit_result(ready(expected), ctx) == ready(expected)


@pytest.mark.parametrize('text', [
    '수학 월요일은 안 하고 수요일 하루 60분',
    '수학 월요일 말고 수요일 하루 60분',
    '수학 월요일 하루 30분, 자료구조는 다음에',
    '수학 월요일 하루 30분 그리고 자료구조는 안 해',
    '수학 월요일 하루 30분 아니 60분',
    '수학 월요일 하루 30분 이상',
    '수학 월요일 하루 30분 또는 60분',
    '수학 월요일 또는 수요일 하루 30분',
    '수학 월요일 하루 30분 정도',
    '수학 월요일 하루 30분이면 좋을까?',
    '수학 월요일 하루 0분',
    '수학 월요일 하루 -30분',
    '수학 월요일 하루 30.5분',
    '수학 월요일 하루 1시간 60분',
    '수학 월요일 하루 25시간',
    '수학 2026-11-21 하루 30분',
    '수학 2026-11-13 하루 30분',
    '수학 2026-02-30 하루 30분',
    '수학 모레부터 2026-11-15일까지 하루 30분',
    '수학 2026-11-18부터 내일까지 하루 30분',
    '수학 월요일 하루 30분 그리고 수학 화요일 하루 30분',
    '전체 월요일 하루 30분 그리고 수학 화요일 하루 30분',
    '물리 월요일 하루 30분',
    '수학 월요일 하루 30분; 목표 90점으로 변경',
    '수학 월요일 하루 30분 기록 삭제해줘',
    '수학 월요일 하루 30분 자료 10쪽 제외해줘',
    '수학 시험일을 11월 20일로 변경해줘',
    '수학 쉽게 나눠줘',
    '수학 월요일 하루 30분 그리고',
])
def test_ambiguous_negative_or_out_of_scope_needs_question(text):
    ctx = context(text)
    assert adapter.explicit_intent(ctx) is None
    with pytest.raises(InvalidOutput):
        adapter.validate_resplit_result(ready([window(A, [16], 30)]), ctx)
    question = {'ready': False, 'question': {'text': '과목, 날짜와 하루 상한을 다시 알려주세요.', 'choices': []}, 'windows': []}
    assert adapter.validate_resplit_result(question, ctx) == question


@pytest.mark.parametrize('mutation', ['owner', 'cap', 'boolean', 'float', 'day', 'missing', 'extra', 'duplicate', 'goal', 'question', 'ready_type'])
def test_provider_cannot_fabricate_or_omit_constraints(mutation):
    ctx = context('수학 월요일 하루 30분')
    value = ready([window(A, [16], 30)])
    if mutation == 'owner': value['windows'][0]['subjectId'] = B
    elif mutation == 'cap': value['windows'][0]['minutesPerDay'] = 60
    elif mutation == 'boolean': value['windows'][0]['minutesPerDay'] = True
    elif mutation == 'float': value['windows'][0]['minutesPerDay'] = 30.0
    elif mutation == 'day': value['windows'][0]['dates'] = ['2026-11-17']
    elif mutation == 'missing': value['windows'] = []
    elif mutation == 'extra': value['windows'].append(window(B, [16], 30))
    elif mutation == 'duplicate': value['windows'][0]['dates'] *= 2
    elif mutation == 'goal': value['windows'][0]['targetScore'] = 90
    elif mutation == 'question': value['question'] = {'text': '질문', 'choices': []}
    elif mutation == 'ready_type': value['ready'] = 1
    with pytest.raises(InvalidOutput):
        adapter.validate_resplit_result(value, ctx)


def test_same_subject_name_asks_instead_of_picking_one():
    ctx = context('수학 월요일 하루 30분')
    ctx['subjects'][1]['name'] = '수학'
    assert adapter.explicit_intent(ctx) is None


@pytest.mark.parametrize('original,answer,expected', [
    ('수학 공부 시간을 줄여줘', '하루 30분', [window(A, range(14, 21), 30)]),
    ('수학 하루 30분으로 배분해줘', '내일부터', [window(A, range(15, 21), 30)]),
    ('수학 평일 하루 30분으로 배분해줘', '내일부터', [window(A, range(16, 21), 30)]),
    ('수학 하루 30분으로 배분해줘', '수학 월요일 하루 30분', [window(A, [16], 30)]),
])
def test_short_followup_fills_missing_constraint_without_erasing_original(original, answer, expected):
    ctx = context(original, answer)
    assert adapter.explicit_intent(ctx) == expected
    assert adapter.validate_resplit_result(ready(expected), ctx) == ready(expected)


@pytest.mark.parametrize('original,answer', [
    ('수학 하루 30분', '하루 60분'),
    ('수학 월요일은 안 해', '하루 30분'),
    ('수학 하루 30분 정도', '월요일'),
    ('수학 내일부터 하루 30분', '오늘부터'),
    ('수학 하루 30분', '자료구조 월요일'),
    ('전체 과목 하루 30분', '수학 월요일'),
    ('수학 월요일 하루 30분', '수요일'),
])
def test_followup_cannot_overwrite_explicit_or_negative_original_constraints(original, answer):
    assert adapter.explicit_intent(context(original, answer)) is None


@pytest.mark.parametrize('question', [None, {'text': ' ', 'choices': []}, {'text': '질문', 'choices': [' ']}])
def test_question_must_be_nonblank_and_cannot_carry_partial_actions(question):
    with pytest.raises(InvalidOutput):
        adapter.validate_resplit_result({'ready': False, 'question': question, 'windows': []}, context('애매해'))
    with pytest.raises(InvalidOutput):
        adapter.validate_resplit_result({'ready': False, 'question': {'text': '질문', 'choices': []},
                                        'windows': [window(A, [16], 30)]}, context('애매해'))


def test_observed_provider_uses_strict_schema_and_closes_after_success(monkeypatch):
    observed = {}
    class Transport:
        def __init__(self, boundary, retry):
            observed.update(boundary=boundary, retry=retry)
        def generate(self, messages, schema, name, max_tokens):
            observed.update(messages=messages, schema=schema, name=name)
            return ready([window(A, [16], 30)])
        def close(self):
            observed['closed'] = True
    monkeypatch.setattr(adapter, 'ObservedGroq', Transport)
    ctx = context('수학 월요일 하루 30분')
    value = adapter.resplit_provider(SimpleNamespace(input=ctx, format_retry=True), 'deadline-boundary')
    assert adapter.validate_resplit_result(value, ctx) == value
    assert observed['boundary'] == 'deadline-boundary' and observed['retry'] is True and observed['closed']
    assert json.loads(observed['messages'][1]['content']) == ctx
    assert observed['schema']['additionalProperties'] is False
    assert set(observed['schema']['required']) == {'ready', 'question', 'windows'}


@pytest.mark.parametrize('error', [RuntimeError('provider unavailable'), json.JSONDecodeError('syntax', '{', 0)])
def test_provider_closes_and_only_delegates_syntax_retry(monkeypatch, error):
    state = []
    class Transport:
        def __init__(self, *args): pass
        def generate(self, *args, **kwargs): raise error
        def close(self): state.append('closed')
    monkeypatch.setattr(adapter, 'ObservedGroq', Transport)
    call = SimpleNamespace(input=context('수학 월요일 하루 30분'), format_retry=False)
    if isinstance(error, json.JSONDecodeError):
        assert adapter.resplit_provider(call, None) == '{'
    else:
        with pytest.raises(RuntimeError): adapter.resplit_provider(call, None)
    assert state == ['closed']


@pytest.mark.parametrize('behavior,success,calls', [('valid', True, 1), ('fabricated', False, 1),
    ('failure', False, 1), ('syntax_once', True, 2), ('syntax_always', False, 2)])
def test_actual_process_gateway_and_observed_groq_transport(behavior, success, calls):
    import multiprocessing
    import time
    from app.ai.gateway import FunctionRef, ProcessExecutor, ProviderSpec, ResultIdentity
    ctx = context('수학 월요일 하루 30분')
    payload = {**ctx, '_fixtureBehavior': behavior}
    identity = ResultIdentity.create(kind='plan_redo', provider='groq', model_version=adapter.MODEL_VERSION,
        prompt_version=adapter.PROMPT_VERSION, schema_version=adapter.SCHEMA_VERSION,
        input=payload, validation_context=ctx)
    result = ProcessExecutor().execute(identity,
        provider=ProviderSpec(FunctionRef('tests.resplit_transport_fixture', 'resplit_transport_provider')),
        validator=adapter.VALIDATOR, deadline=time.monotonic() + 20, cleanup_timeout=0.5)
    assert (result.failure is None) is success
    assert len(result.usage) == calls
    assert not multiprocessing.active_children()
    if success:
        assert result.data == ready([window(A, [16], 30)])
    if behavior != 'failure':
        assert all(item.input_tokens == 13 and item.output_tokens == 7 for item in result.usage)
        assert all(item.provider_request_id == 'fixture-groq-request' for item in result.usage)
    else:
        assert result.usage[0].status == 'failed'


def test_comma_separates_subject_specific_constraints():
    ctx = context('수학 월요일 하루 30분, 자료구조 화요일 하루 45분')
    expected = [window(A, [16], 30), window(B, [17], 45)]
    assert adapter.explicit_intent(ctx) == expected
    assert adapter.validate_resplit_result(ready(expected), ctx) == ready(expected)


@pytest.mark.parametrize('text', ['수학은 월요일 자료구조는 화요일 하루 30분',
    '수학 월수금 자료구조 화요일 하루 30분', '수학 월/수/금 자료구조 화요일 하루 30분'])
def test_subject_following_another_subject_constraint_cannot_inherit_it(text):
    ctx = context(text)
    assert adapter.explicit_intent(ctx) is None
    with pytest.raises(InvalidOutput):
        adapter.validate_resplit_result(ready([window(A, [16, 17], 30), window(B, [16, 17, 23], 30)]), ctx)
