from copy import deepcopy

import pytest

from app.ai.material_adapter import quality_report, validate_material_result
from app.ai.gateway import InvalidOutput


def analysis_result(*, method='native', readable=True):
    pages = [{'page': 1, 'text': 'Study material evidence ' * 5 if readable else ''}]
    return {'schemaVersion': 'material-analysis-result-v1', 'sourceSha256': 'a' * 64,
        'analysisVersion': 'v1', 'totalPages': 1, 'method': method, 'pages': pages,
        'quality': quality_report(pages, 1),
        'units': [{'title': 'Unit', 'start_page': 1, 'end_page': 1, 'difficulty': 'mid',
                   'evidence': ['PDF page 1'], 'concepts': [{'title': 'Concept', 'start_page': 1, 'end_page': 1}]}] if readable else []}


def context(result):
    return {key: result[key] for key in ('sourceSha256', 'analysisVersion', 'totalPages')}


@pytest.mark.parametrize('method,readable', [('native', True), ('ocr', True), ('ocr', False)])
def test_quality_keeps_successful_ocr_separate_from_quizless(method, readable):
    value = analysis_result(method=method, readable=readable)
    assert validate_material_result(value, context(value)) == value
    assert value['quality']['quiz_allowed'] is readable


@pytest.mark.parametrize('mutation', ['sha', 'version', 'pages', 'quality', 'empty', 'overlap', 'outside', 'concept', 'bool'])
def test_invalid_analysis_cannot_replace_structure(mutation):
    original = analysis_result()
    value = deepcopy(original)
    if mutation == 'sha': value['sourceSha256'] = 'b' * 64
    if mutation == 'version': value['analysisVersion'] = 'other'
    if mutation == 'pages': value['totalPages'] = 2
    if mutation == 'quality': value['quality']['quiz_allowed'] = False
    if mutation == 'empty': value['units'] = []
    if mutation == 'overlap': value['units'] *= 2
    if mutation == 'outside': value['units'][0]['end_page'] = 2
    if mutation == 'concept': value['units'][0]['concepts'][0]['start_page'] = 0
    if mutation == 'bool': value['units'][0]['start_page'] = True
    with pytest.raises((InvalidOutput, ValueError)):
        validate_material_result(value, context(original))
