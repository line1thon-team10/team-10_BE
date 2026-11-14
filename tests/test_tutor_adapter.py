from copy import deepcopy

import pytest

from app.ai.gateway import InvalidOutput
from app.ai.tutor_adapter import selected_sources, validate_tutor_result


def evidence():
    return {'question': 'AVL rotation', 'parent': None, 'materials': [
        {'id': 'owned', 'subjectId': 'subject', 'subjectName': 'Data structures', 'pageCount': 2,
         'pages': [{'page': 1, 'text': 'AVL rotation balances a search tree.'}, {'page': 2, 'text': 'Binary search.'}]}]}


def answer():
    return {'subjectId': 'subject', 'answer': 'AVL rotation balances a search tree.',
        'sources': [{'materialId': 'owned', 'from': 1, 'to': 1, 'quote': 'AVL rotation balances a search tree.'}]}


def test_actual_owned_text_is_required_for_citation():
    assert validate_tutor_result(answer(), evidence()) == answer()


@pytest.mark.parametrize('error', ['no_source', 'foreign', 'outside', 'wrong_subject', 'fabricated', 'duplicate', 'boolean'])
def test_invalid_references_cannot_be_saved(error):
    value = answer()
    if error == 'no_source': value['sources'] = []
    if error == 'foreign': value['sources'][0]['materialId'] = 'foreign'
    if error == 'outside': value['sources'][0].update({'from': 3, 'to': 3})
    if error == 'wrong_subject': value['subjectId'] = 'other'
    if error == 'fabricated': value['sources'][0]['quote'] = 'invented evidence'
    if error == 'duplicate': value['sources'] *= 2
    if error == 'boolean': value['sources'][0]['from'] = True
    with pytest.raises(InvalidOutput):
        validate_tutor_result(value, evidence())


def test_easy_cannot_expand_parent_evidence():
    context = evidence()
    context['parent'] = {'question': 'AVL rotation', 'answer': 'Original', 'subjectId': 'subject',
                         'pages': [{'materialId': 'owned', 'from': 1, 'to': 1}]}
    assert validate_tutor_result(answer(), context) == answer()
    value = answer()
    value['sources'][0].update({'from': 2, 'to': 2, 'quote': 'Binary search.'})
    with pytest.raises(InvalidOutput):
        validate_tutor_result(value, context)


def test_subject_selection_does_not_silently_guess_ambiguous_subject():
    context = evidence()
    second = deepcopy(context['materials'][0])
    second.update(id='other', subjectId='other')
    context['materials'].append(second)
    with pytest.raises(InvalidOutput):
        selected_sources(context)
