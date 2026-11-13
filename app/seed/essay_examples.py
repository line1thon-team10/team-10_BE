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


def saved_evaluation(question, answer):
    if (question.get("code") == DEMO_ESSAY["code"]
            and question.get("generation_version") == DEMO_ESSAY["generation_version"]
            and evaluation_key(question.get("prompt"), question.get("correct_answer"), answer) == DEMO_ESSAY_KEY):
        return DEMO_ESSAY["score"], list(DEMO_ESSAY["good"]), list(DEMO_ESSAY["missing"]), "fallback"
    return None
