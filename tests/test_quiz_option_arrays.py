from tests.test_quiz_answer_api import answer_case


def test_all_quiz_options_are_arrays(api_client, answer_case):
    case = answer_case
    response = api_client.get(f"/api/quiz/{case['quiz']}", headers={'X-Anonymous-Id': case['user']})
    assert response.status_code == 200, response.text
    questions = response.json()['data']['questions']
    assert {'choice', 'short', 'essay'} <= {question['type'] for question in questions}
    for question in questions:
        assert isinstance(question['options'], list)
        assert bool(question['options']) == (question['type'] == 'choice')
