"""고정 AI 패키지를 로컬에서 검증하고 데모 DB 입력으로 변환한다. 외부 API 호출 없음."""
import hashlib
import json

import pymupdf

from app.ai import engine
from app.config import AI_ENGINE_PATH
from app.core.responses import ApiError
from app.seed import demo_data as d
from app.seed.package_questions import QUESTIONS

LEVELS = {"안정": "stable", "흔들림": "shaky", "붕괴 위기": "risk"}


def load_package():
    subjects, units, concepts, questions = [], [], [], []
    try:
        for definition in d.SUBJECTS:
            asset, code = definition["asset"], definition["code"]
            root = AI_ENGINE_PATH / "data"
            meta = json.loads((root / "ground_truth" / f"{asset}.json").read_text())
            pdf_path = root / "demo" / f"{asset}.pdf"
            pdf_bytes = pdf_path.read_bytes()
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
                if len(pdf) != meta["total_pages"] or len(pdf) != 30:
                    raise ValueError("PDF page count differs from the demo contract")
                subject = {**definition, "file": (f"{asset}.pdf", len(pdf)),
                           "sha256": hashlib.sha256(pdf_bytes).hexdigest(), "units": [],
                           "raw_goal": f"{definition['name']} 목표 {definition['target_grade']}",
                           "passing_criteria": f"시연 목표 {definition['target_score']}점",
                           "grading_weights": None, "emphasized_content": None}
                for no, section in enumerate(meta["sections"], 1):
                    uid = f"{code}{no}"
                    start, end = section["start_page"], section["end_page"]
                    if (start, end) != (3 + (no - 1) * 9, 11 + (no - 1) * 9):
                        raise ValueError("Demo unit range changed")
                    subject["units"].append((section["section_title"], start, end, "low", []))
                    units.append({"id": uid, "subject": code, "name": section["section_title"],
                                  "page_start": start, "page_end": end, "difficulty": "하"})
                    topic_by_page = {topic["page"]: topic for topic in section["topics"]}
                    for page, topic in topic_by_page.items():
                        if not start <= page <= end:
                            raise ValueError("Concept outside unit")
                        concepts.append({"id": f"{uid}-P{page}", "unit": uid, "name": topic["title"],
                                         "page_start": page, "page_end": page})
                    for i, (page, kind, prompt, answer, choices) in enumerate(QUESTIONS[uid], 1):
                        if page not in topic_by_page or not start <= page <= end:
                            raise ValueError("Question outside unit")
                        text = pdf[page - 1].get_text()
                        quote = " ".join(text.split("핵심 개념\n", 1)[1].split("적용 사례\n", 1)[0].split())
                        questions.append({"id": f"{uid}-Q{i:02}", "unit": uid, "concept": f"{uid}-P{page}",
                                          "type": kind, "difficulty": "하", "question": prompt, "answer": answer,
                                          "choices": choices, "page": page, "quote": quote,
                                          "explanation": quote, "source": "daily"})
                if len(subject["units"]) != 3:
                    raise ValueError("Demo requires three units per subject")
                subjects.append(subject)
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise ApiError("DEMO_PACKAGE_INVALID", "데모 패키지를 확인하지 못했어요", 500) from exc

    # 11/8까지의 실제 DB에 저장할 시연 풀이. 점수를 별도로 하드코딩하지 않는다.
    attempts = []
    for code, day, correct_count in (("AI", "2026-11-03", 5), ("DB", "2026-11-04", 2), ("OS", "2026-11-05", 4)):
        for i in range(1, 6):
            q = next(q for q in questions if q["id"] == f"{code}1-Q{i:02}")
            correct = i <= correct_count
            attempts.append({"date": day, "question": q["id"], "correct": correct,
                             "confidence": "애매" if code == "AI" and i == 5 else "확실",
                             "answer": q["answer"] if correct else ("X" if q["answer"] == "O" else "O" if q["answer"] == "X" else "모르겠어요")})
    weak = {a["question"] for a in attempts if not a["correct"] or a["confidence"] == "애매"}
    quiz = {"subjects": [{"id": s["code"], "name": s["name"], "pages": 27,
                          "target": s["target_score"], "exam": s["exam_date"].isoformat()} for s in subjects],
            "units": units, "concepts": concepts, "questions": questions, "attempts": attempts,
            "pending_quizzes_1109": {s["code"]: [q["id"] for q in questions if q["id"] in weak and q["unit"].startswith(s["code"])]
                                    + [q["id"] for q in questions if q["unit"] == s["code"] + "2"] for s in subjects}}
    results = engine.module("learning_metrics").calculate_learning_metrics(quiz)
    mastery = {u["unit"]: (u["score"], LEVELS[u["grade"]], u["recovery_mode"]) for u in results["units"]}
    return subjects, quiz, mastery
