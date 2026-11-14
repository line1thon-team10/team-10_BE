"""키 없는 시연용으로 명시적으로 작성한 평가 예시. 실시간 AI 채점 결과가 아니다.

문항 내용과 입력 답안 전체가 일치할 때만 재사용한다. 예시는 현재 문항에서
자동 생성하지 않아 문항 변경 후 오래된 평가가 새 문항에 적용되지 않게 한다.
"""
import hashlib
import json


def evaluation_key(prompt, correct_answer, answer):
    return hashlib.sha256(json.dumps([prompt, correct_answer, answer],
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


DEMO_ESSAY = {
    "code": "AI2-Q05",
    "generation_version": "demo-package-2026-11-14-v2",
    "prompt": "로지스틱 회귀에서 선형 결합 결과를 어떻게 분류 확률로 바꾸는지 설명하세요.",
    "correct_answer": "선형 결합 결과를 시그모이드 함수 등에 통과시켜 분류 확률을 추정한다.",
    "answer": "선형 결합 결과로 분류 확률을 추정합니다.",
    "score": 70,
    "good": ["선형 결합 결과를 사용해 분류 확률을 추정한다는 설명이 맞아요."],
    "missing": ["선형 결합 결과를 시그모이드 함수 등에 통과시키는 변환 과정을 보완해 주세요."],
}
DEMO_ESSAY_KEY = evaluation_key(DEMO_ESSAY["prompt"], DEMO_ESSAY["correct_answer"], DEMO_ESSAY["answer"])

# Reviewed authored fixture compatibility, not evidence of a provider call.
# Keep these pins independent from the running adapter so an adapter/PDF update
# cannot silently certify the old evaluation for a new input or version.
DEMO_ESSAY_VERSIONS = {
    "model_version": "openai/gpt-oss-120b",
    "prompt_version": "essay-6d74ffb6c455a306e912252013d76f93e30a6210-adapter-v1",
    "schema_version": "essay-grade-v1",
}
DEMO_ESSAY_QUOTE = "로지스틱 회귀는 선형 결합 결과를 시그모이드 함수 등에 통과시켜 분류 확률을 추정한다."
DEMO_ESSAY_PDF_HASH = "9afd8097c645557fabd0817eb0c56608b2a367e7125263e63319b84a15c7ca82"


def register_saved_evaluation(conn, user_id, question_id):
    """Seed-only registration in its caller transaction; never called on a miss."""
    from app.ai.grading_adapter import validate_essay_result
    from app.ai.gateway import parse_json
    from app.repositories.ai_results import AiResultRepository
    from app.services.grading import capture_question_context, essay_request_for_input

    context = capture_question_context(conn, user_id, question_id)
    question, sources = context["question"], context["sources"]
    material_context = context["material_context"]
    materials, unit, concept = (material_context[key] for key in ("materials", "unit", "concept"))
    expected_question = {key: DEMO_ESSAY[key] for key in ("code", "generation_version", "prompt", "correct_answer")}
    expected_question.update(type="essay", difficulty="low", choices=None, explanation=DEMO_ESSAY_QUOTE, origin="seed")
    if any(question.get(key) != value for key, value in expected_question.items()):
        return []
    if len(materials) != 1 or len(sources) != 1 or concept is None:
        return []
    material, source = materials[0], sources[0]
    expected_material = {"sha256": DEMO_ESSAY_PDF_HASH, "analysis_version": DEMO_ESSAY["generation_version"],
                         "source_format": "pdf", "page_count": 30}
    expected_source = {"question_id": question_id, "material_id": material["id"],
                       "start_page": 13, "end_page": 13, "quote": DEMO_ESSAY_QUOTE}
    expected_unit = {"material_id": material["id"], "quantity_basis": "page", "start_page": 12,
                     "end_page": 20, "in_scope": True, "retired_at": None}
    expected_concept = {"unit_id": unit["id"], "start_page": 13, "end_page": 13, "in_scope": True, "retired_at": None}
    if any(actual.get(key) != value for actual, expected in (
            (material, expected_material), (source, expected_source), (unit, expected_unit),
            (concept, expected_concept)) for key, value in expected.items()):
        return []
    saved_ids = []
    repository = AiResultRepository(conn.engine)
    for confidence in ("sure", "unsure"):
        request = essay_request_for_input(user_id, context, text=DEMO_ESSAY["answer"], confidence=confidence)
        if any(getattr(request.identity, key) != value for key, value in DEMO_ESSAY_VERSIONS.items()):
            return []
        value = validate_essay_result({"score": DEMO_ESSAY["score"], "strengths": list(DEMO_ESSAY["good"]),
            "missing_points": list(DEMO_ESSAY["missing"]),
            "feedback": " ".join(DEMO_ESSAY["good"] + DEMO_ESSAY["missing"])},
            parse_json(request.identity.validation_json))
        saved_ids.append(repository.save_in_transaction(conn, request, value))
    return saved_ids


def saved_evaluation(question, answer):
    if (question.get("code") == DEMO_ESSAY["code"]
            and question.get("generation_version") == DEMO_ESSAY["generation_version"]
            and evaluation_key(question.get("prompt"), question.get("correct_answer"), answer) == DEMO_ESSAY_KEY):
        return DEMO_ESSAY["score"], list(DEMO_ESSAY["good"]), list(DEMO_ESSAY["missing"]), "fallback"
    return None
