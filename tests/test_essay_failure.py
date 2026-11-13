from unittest.mock import patch

import pytest

from app.core.responses import ApiError
from app.services.quiz import QuizService


@pytest.mark.parametrize("text", ["답안", "  답안  "])
def test_failed_grading_never_becomes_zero_score(text):
    service = object.__new__(QuizService)
    with patch("app.services.quiz.ai.module", side_effect=RuntimeError("offline")):
        with pytest.raises(ApiError) as error:
            service._grade_essay({"code": "new-question"}, text)
    assert error.value.code == "AI_FAILED"
    assert error.value.status == 503


def test_blank_answer_is_zero_without_ai():
    service = object.__new__(QuizService)
    with patch("app.services.quiz.ai.module") as module:
        assert service._grade_essay({}, "  ")[0] == 0
        module.assert_not_called()


def test_exact_demo_evaluation_is_reused():
    from app.seed.essay_examples import DEMO_ESSAY
    service = object.__new__(QuizService)
    with patch("app.services.quiz.ai.module", side_effect=RuntimeError("offline")):
        result = service._grade_essay(DEMO_ESSAY, DEMO_ESSAY["answer"])
    assert result[0] == 70 and result[3] == "fallback"


@pytest.mark.parametrize("change", ["answer", "prompt", "correct_answer", "generation_version", "code"])
def test_changed_content_never_reuses_demo_evaluation(change):
    from app.seed.essay_examples import DEMO_ESSAY
    question = dict(DEMO_ESSAY)
    question[change] += " changed"
    service = object.__new__(QuizService)
    with patch("app.services.quiz.ai.module", side_effect=RuntimeError("offline")):
        with pytest.raises(ApiError) as error:
            service._grade_essay(question, question["answer"])
    assert error.value.code == "AI_FAILED"


def test_old_question_code_does_not_grant_a_canned_grade():
    service = object.__new__(QuizService)
    with patch("app.services.quiz.ai.module", side_effect=RuntimeError("offline")):
        with pytest.raises(ApiError):
            service._grade_essay({"code": "DS3-06"}, "답안")
