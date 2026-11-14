"""오늘의 퀴즈 (QZ) · 이해도 반영 (LV).

- 문항 구성 (4-3): 오늘 공부한 단원마다 새 문제 5 + 다시 볼 취약 문항 전부
- 채점: 객관식·OX·단답형은 서버, 서술형은 AI(85점 이상 정답). AI가 안 되면 저장된 채점으로 (NF-10)
- 단원 점수: 현민님 learning_metrics(calc.py와 같은 식)에 전체 풀이 기록을 넣어 다시 계산해요
"""
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import Connection, func, select, update

from app.ai import engine as ai
from app.core.responses import ApiError
from app.db import schema as t
from app.services.study import StudyData, unit_no
from app.seed.essay_examples import saved_evaluation

NEW_PER_UNIT = 5
MORE_COUNT = 5
GRADE_FROM_AI = {"안정": "stable", "흔들림": "shaky", "붕괴 위기": "risk", "완벽": "perfect"}
DIFF_KO = {"low": "하", "mid": "중", "high": "상"}
TYPE_KO = {"choice": "객관식", "ox": "OX", "short": "단답형", "essay": "서술형"}
CONF_KO = {"sure": "확실", "unsure": "애매"}
TAGS = {"weak": "어제 오답", "carried_over": "어제 안 푼 문제", "extra": "더 풀기", "practice": "문제 뽑기"}

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(s) -> str:
    return re.sub(r"[.,!?。\s]", "", str(s or "")).lower()


class QuizService:
    def __init__(self, conn: Connection, user: dict, today: date):
        self.conn = conn
        self.user = user
        self.today = today
        self.data = StudyData(conn, user["id"])

    # ---------- 조회 ----------
    def session(self, key: str) -> dict:
        """key = 퀴즈 ID(더 풀기 등) 또는 과목 ID(그 과목의 오늘 퀴즈)"""
        s = self.conn.execute(select(t.quiz_sessions).where(
            t.quiz_sessions.c.id == key, t.quiz_sessions.c.user_id == self.user["id"])).mappings().first()
        if s:
            return dict(s)
        if key not in self.data.subject_by_id:
            raise ApiError("NO_QUIZ", "퀴즈를 찾지 못했어요", 404)
        return self.ensure_daily(key)

    def daily_sessions(self, subject_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(select(t.quiz_sessions).where(
            t.quiz_sessions.c.user_id == self.user["id"], t.quiz_sessions.c.subject_id == subject_id,
            t.quiz_sessions.c.quiz_date == self.today, t.quiz_sessions.c.type == "daily")
            .order_by(t.quiz_sessions.c.created_at)).mappings()]

    def items(self, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            select(t.quiz_items.c.id.label("item_id"), t.quiz_items.c.question_id,
                   t.quiz_items.c.sort_order, t.quiz_items.c.inclusion_reason,
                   *(column for column in t.questions.c if column.name != "id"),
                   t.quiz_attempts.c.id.label("attempt_id"), t.quiz_attempts.c.answer,
                   t.quiz_attempts.c.confidence, t.quiz_attempts.c.is_correct, t.quiz_attempts.c.essay_score,
                   t.quiz_attempts.c.got_points, t.quiz_attempts.c.missing_points,
                   t.quiz_attempts.c.grading_status)
            .join(t.questions, t.quiz_items.c.question_id == t.questions.c.id)
            .outerjoin(t.quiz_attempts, t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id)
            .where(t.quiz_items.c.quiz_session_id == session_id).order_by(t.quiz_items.c.sort_order)).mappings()
        out = [dict(r) for r in rows]
        if out:
            srcs = {}
            for r in self.conn.execute(select(t.question_sources).where(
                    t.question_sources.c.question_id.in_([x["question_id"] for x in out]))).mappings():
                srcs.setdefault(r["question_id"], dict(r))
            for x in out:
                x["source"] = srcs.get(x["question_id"])
        return out

    # ---------- 구성 ----------
    def ensure_daily(self, subject_id: str) -> dict:
        """그 과목의 오늘 퀴즈. 없으면 규칙대로 만들어요 (새 문제 5/단원 + 취약 문항)"""
        open_ones = [s for s in self.daily_sessions(subject_id) if s["status"] in ("ready", "in_progress")]
        if open_ones:
            return open_ones[0]
        done = [s for s in self.daily_sessions(subject_id) if s["status"] == "submitted"]
        if done:
            return done[-1]
        studied_units = []
        for x in self.data.tasks_on(self.today):
            unit = self.data.unit_of(x)
            if x.row["subject_id"] == subject_id and unit and unit["id"] not in studied_units:
                studied_units.append(unit["id"])
        if not studied_units:
            raise ApiError("NO_QUIZ", "오늘 이 과목은 공부한 범위가 없어서 퀴즈가 없어요", 404)
        attempted = set(self.conn.execute(
            select(t.quiz_items.c.question_id).join(t.quiz_sessions, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .where(t.quiz_sessions.c.user_id == self.user["id"])).scalars())
        chosen: list[tuple[str, str]] = []
        weak = self.conn.execute(
            select(t.weak_questions.c.question_id).join(t.questions, t.weak_questions.c.question_id == t.questions.c.id)
            .join(t.units, t.questions.c.unit_id == t.units.c.id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .where(t.materials.c.subject_id == subject_id, t.weak_questions.c.status == "active",
                   t.weak_questions.c.next_review_date <= self.today)).scalars().all()
        chosen += [(q, "weak") for q in weak]
        for uid in studied_units:
            fresh = self.conn.execute(
                select(t.questions.c.id).where(t.questions.c.unit_id == uid, t.questions.c.difficulty == "low",
                                               t.questions.c.origin != "generated")
                .order_by(t.questions.c.code)).scalars().all()
            chosen += [(q, "new") for q in [q for q in fresh if q not in attempted][:NEW_PER_UNIT]]
        if not chosen:
            raise ApiError("NO_QUESTIONS", "이 범위 문항이 아직 준비되지 않았어요", 404)
        return self._create(subject_id, "daily", chosen)

    def _create(self, subject_id, qtype, chosen, parent=None) -> dict:
        sid = str(uuid.uuid4())
        self.conn.execute(t.quiz_sessions.insert().values(
            id=sid, user_id=self.user["id"], subject_id=subject_id, parent_quiz_session_id=parent, type=qtype,
            quiz_date=self.today, status="ready", selected_difficulty="low", current_position=0,
            grading_status="pending", created_at=_now()))
        for i, (qid, reason) in enumerate(chosen):
            self.conn.execute(t.quiz_items.insert().values(
                id=str(uuid.uuid4()), quiz_session_id=sid, question_id=qid, sort_order=i + 1,
                inclusion_reason=reason))
        return dict(self.conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.id == sid)).mappings().one())

    def make_more(self, subject_id: str, unit_number: int) -> dict:
        base = next((s for s in reversed(self.daily_sessions(subject_id)) if s["status"] == "submitted"), None)
        if not base:
            raise ApiError("NO_RESULT", "오늘 퀴즈를 먼저 풀어 주세요")
        unit = next((u for u in self.data.units.values()
                     if u["subject_id"] == subject_id and unit_no(u["code"]) == unit_number), None)
        if not unit:
            raise ApiError("UNIT_NOT_FOUND", "단원을 찾지 못했어요", 404)
        already = self.conn.execute(select(t.quiz_sessions.c.id).join(
            t.quiz_scopes, t.quiz_scopes.c.quiz_session_id == t.quiz_sessions.c.id).where(
            t.quiz_sessions.c.parent_quiz_session_id == base["id"], t.quiz_scopes.c.unit_id == unit["id"])).first()
        if already:
            raise ApiError("ALREADY", "이 단원은 오늘 더 풀기를 했어요")
        attempted = set(self.conn.execute(
            select(t.quiz_items.c.question_id).join(t.quiz_sessions, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .where(t.quiz_sessions.c.user_id == self.user["id"])).scalars())
        pool = [q for q in self.conn.execute(select(t.questions.c.id).where(
            t.questions.c.unit_id == unit["id"], t.questions.c.difficulty == "low").order_by(t.questions.c.code)).scalars()
            if q not in attempted][:MORE_COUNT]
        if not pool:
            raise ApiError("NO_QUESTIONS", "이 단원은 더 풀 문항이 없어요", 404)
        s = self._create(subject_id, "extra", [(q, "extra") for q in pool], parent=base["id"])
        self.conn.execute(t.quiz_scopes.insert().values(
            id=str(uuid.uuid4()), quiz_session_id=s["id"], unit_id=unit["id"],
            start_page=unit["start_page"] or 0, end_page=unit["end_page"] or 0))
        return {"quizId": s["id"], "count": len(pool)}

    # ---------- 화면 모양 ----------
    def round_of(self, items) -> str:
        """새로 푸는 단원 기준 회독 (취약 문항 단원은 빼요)"""
        fresh = {x["unit_id"] for x in items if x["inclusion_reason"] in ("new", "extra", "practice")}
        return self._round(fresh or {x["unit_id"] for x in items})

    def _round(self, unit_ids) -> str:
        passes = self.conn.execute(select(func.max(t.learning_passes.c.pass_number)).where(
            t.learning_passes.c.unit_id.in_(list(unit_ids)), t.learning_passes.c.invalidated_at.is_(None))).scalar()
        return f"{max(1, passes or 1)}회독"

    def composition(self, items) -> tuple[str, list[str]]:
        by_unit = defaultdict(list)
        weak = [x for x in items if x["inclusion_reason"] in ("weak", "carried_over")]
        for x in items:
            if x["inclusion_reason"] in ("new", "extra"):
                by_unit[x["unit_id"]].append(x)
        lines = []
        for uid, xs in by_unit.items():
            u = self.data.units.get(uid)
            lines.append(f"{unit_no(u['code'])}단원 새 문제 {len(xs)}" if u else f"새 문제 {len(xs)}")
        if weak:
            lines.append(f"어제 틀린 문제 {len(weak)}")
        return " + ".join(lines), lines

    def question_view(self, x: dict) -> dict:
        qtype = "choice" if x["type"] in ("choice", "ox") else x["type"]  # 화면은 OX도 객관식 모양
        u = self.data.units.get(x["unit_id"])
        src = x.get("source")
        return {"id": x["item_id"], "type": qtype, "tag": TAGS.get(x["inclusion_reason"]),
                "unitNo": unit_no(u["code"]) if u else None,
                "page": f"p.{src['start_page']}" if src else None, "prompt": x["prompt"],
                "options": [c["text"] for c in x["choices"]] if x["choices"] else None}

    def set_view(self, session: dict, saved: bool = False) -> dict:
        items = self.items(session["id"])
        subject = self.data.subject_by_id.get(session["subject_id"], {})
        comp, _ = self.composition(items)
        return {
            "saved": saved or session["is_fallback"], "subjectId": session["subject_id"] if session["type"] == "daily"
            else session["id"], "custom": session["type"] != "daily",
            "resultFor": session["subject_id"] if session["type"] != "daily" else session["subject_id"],
            "status": session["status"], "name": subject.get("name"),
            "round": self.round_of(items), "difficulty": DIFF_KO.get(session["selected_difficulty"], "하"),
            "composition": comp, "questions": [self.question_view(x) for x in items],
        }

    # ---------- 채점 ----------
    def _grade_essay(self, q: dict, text: str):
        if not text.strip():
            return 0, [], ["답안을 입력하지 않았어요."], "graded"
        src = q.get("source") or {}
        try:
            r = ai.module("essay_grader").grade_essay(
                {"id": q["code"] or q["question_id"], "question": q["prompt"], "answer": q["correct_answer"],
                 "page": src.get("start_page") or 1, "source_text": src.get("quote") or ""}, text)
            return r["score"], r["strengths"], r["missing_points"], "graded"
        except Exception:  # noqa: BLE001 — AI 실패·키 없음은 저장된 채점으로 (NF-10)
            saved = saved_evaluation(q, text)
            if saved is not None:
                return saved
            raise ApiError("AI_FAILED", "서술형 채점을 하지 못했어요. 답안을 유지한 채 다시 제출해 주세요.", 503)

    def submit(self, session: dict, answers: list[dict]) -> None:
        if session["status"] == "submitted":
            raise ApiError("QUIZ_FINALIZED", "이미 제출한 퀴즈예요", 409)
        items = self.items(session["id"])
        by_item = {a["questionId"]: a for a in answers}
        if any(not by_item.get(x["item_id"], {}).get("confidence") for x in items):
            raise ApiError("CONFIDENCE_REQUIRED", "모든 문항에 확실/애매를 골라 주세요")
        now = _now()
        fallback = False
        for x in items:
            a = by_item[x["item_id"]]
            score, good, miss, status = None, None, None, "graded"
            if x["type"] in ("choice", "ox"):
                idx = a.get("choice")
                picked = x["choices"][idx]["text"] if isinstance(idx, int) and 0 <= idx < len(x["choices"] or []) else None
                answer, correct = str(picked or ""), _norm(picked) == _norm(x["correct_answer"])
            elif x["type"] == "short":
                answer = a.get("text") or ""
                correct = bool(answer.strip()) and _norm(answer) == _norm(x["correct_answer"])
            else:
                answer = a.get("text") or ""
                score, good, miss, status = self._grade_essay(x, answer)
                correct = score >= 85
                fallback = fallback or status == "fallback"
            self.conn.execute(t.quiz_attempts.insert().values(
                id=str(uuid.uuid4()), quiz_item_id=x["item_id"], answer=answer, confidence=a["confidence"],
                is_correct=correct, grading_status=status, essay_score=score, got_points=good, missing_points=miss,
                answered_at=now, answered_date=self.today, finalized_at=now, graded_at=now, mastery_applied_at=now))
            self._update_weak(x["question_id"], correct and a["confidence"] == "sure", now)
        self.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == session["id"]).values(
            status="submitted", current_position=len(items), grading_status="completed", is_fallback=fallback,
            submitted_at=now, finalized_at=now, graded_at=now))
        self._recalc_mastery(session["id"], {x["unit_id"] for x in items})

    def _update_weak(self, question_id: str, sure_correct: bool, now) -> None:
        """취약 문항: 1일 후 → 3일 후 두 번 다 확실로 맞히면 해제, 중간에 틀리면 처음부터 (4-3)"""
        w = self.conn.execute(select(t.weak_questions).where(t.weak_questions.c.question_id == question_id)).mappings().first()
        if not sure_correct:
            values = dict(status="active", review_step=0, next_review_date=self.today.fromordinal(self.today.toordinal() + 1),
                          last_weak_at=now, resolved_at=None)
            if w:
                self.conn.execute(update(t.weak_questions).where(t.weak_questions.c.question_id == question_id).values(**values))
            else:
                self.conn.execute(t.weak_questions.insert().values(question_id=question_id, **values))
        elif w and w["status"] == "active":
            step = w["review_step"] + 1
            values = (dict(status="resolved", review_step=2, next_review_date=None, resolved_at=now) if step >= 2 else
                      dict(review_step=step, next_review_date=self.today.fromordinal(self.today.toordinal() + 3)))
            self.conn.execute(update(t.weak_questions).where(t.weak_questions.c.question_id == question_id).values(**values))

    def _metrics_input(self) -> dict:
        """DB 기록을 현민님 learning_metrics 입력 모양(seed_quiz.json과 같은 키)으로"""
        units = [u for u in self.data.units.values()]
        code = {u["id"]: u["code"] for u in units}
        concepts = self.conn.execute(select(t.concepts).where(t.concepts.c.unit_id.in_(list(code)))).mappings().all()
        ccode = {c["id"]: c["code"] for c in concepts}
        qs = self.conn.execute(select(t.questions).where(t.questions.c.unit_id.in_(list(code)))).mappings().all()
        qcode = {q["id"]: q["code"] or q["id"] for q in qs}
        attempts = self.conn.execute(
            select(t.quiz_attempts.c.answered_date, t.quiz_attempts.c.confidence, t.quiz_attempts.c.is_correct,
                   t.quiz_attempts.c.essay_score, t.quiz_items.c.question_id)
            .join(t.quiz_items, t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id)
            .where(t.quiz_items.c.question_id.in_(list(qcode)), t.quiz_attempts.c.is_correct.is_not(None))
            .order_by(t.quiz_attempts.c.answered_at)).mappings().all()
        return {
            "subjects": [{"id": s["code"], "name": s["name"], "pages": 1, "target": s["target_score"] or 0,
                          "exam": s["exam_date"].isoformat() if s["exam_date"] else None} for s in self.data.subjects],
            "units": [{"id": u["code"], "subject": self.data.subject_by_id[u["subject_id"]]["code"], "name": u["title"],
                       "page_start": u["start_page"], "page_end": u["end_page"],
                       "difficulty": DIFF_KO.get(u["difficulty"], "하")} for u in units],
            "concepts": [{"id": c["code"], "unit": code[c["unit_id"]], "name": c["title"],
                          "page_start": c["start_page"], "page_end": c["end_page"]} for c in concepts],
            "questions": [{"id": qcode[q["id"]], "concept": ccode.get(q["concept_id"]), "unit": code[q["unit_id"]],
                           "type": TYPE_KO[q["type"]], "difficulty": DIFF_KO.get(q["difficulty"], "하")} for q in qs],
            "attempts": [{"date": a["answered_date"].isoformat(), "question": qcode[a["question_id"]],
                          "confidence": CONF_KO[a["confidence"]], "correct": a["is_correct"],
                          **({"essay_score": a["essay_score"]} if a["essay_score"] is not None else {})} for a in attempts],
        }

    def _recalc_mastery(self, quiz_session_id: str, unit_ids: set[str]) -> None:
        result = ai.module("learning_metrics").calculate_learning_metrics(self._metrics_input())
        by_code = {u["unit"]: u for u in result["units"]}
        now = _now()
        for uid in unit_ids:
            u = self.data.units[uid]
            r = by_code.get(u["code"])
            if not r:
                continue
            level = GRADE_FROM_AI.get(r["grade"], "shaky")
            self.conn.execute(update(t.units).where(t.units.c.id == uid).values(
                mastery_score=r["score"], mastery_level=level, recovery_mode=r["recovery_mode"],
                recovery_started_at=u["recovery_started_at"] or (now if r["recovery_mode"] else None),
                mastery_updated_at=now, updated_at=now))
            self.conn.execute(t.mastery_history.insert().values(
                id=str(uuid.uuid4()), unit_id=uid, quiz_session_id=quiz_session_id,
                previous_score=u["mastery_score"], previous_level=u["mastery_level"], raw_score=r["score"],
                score=r["score"], level=level, recovery_mode=r["recovery_mode"], recorded_date=self.today,
                created_at=now))

    # ---------- 결과 ----------
    def result_view(self, session: dict) -> dict:
        if session["status"] != "submitted":
            raise ApiError("NO_RESULT", "아직 푼 퀴즈가 없어요", 404)
        data = StudyData(self.conn, self.user["id"])  # 채점 뒤 점수로 다시 읽어요
        items = self.items(session["id"])
        out_items = []
        for i, x in enumerate(items):
            src = x.get("source")
            essay = x["type"] == "essay"
            out_items.append({
                "questionId": x["item_id"], "no": i + 1, "unitNo": unit_no(data.units[x["unit_id"]]["code"]),
                "type": "choice" if x["type"] in ("choice", "ox") else x["type"], "tag": TAGS.get(x["inclusion_reason"]),
                "prompt": x["prompt"], "page": f"p.{src['start_page']}" if src else None,
                "confidence": x["confidence"], "correct": bool(x["is_correct"]),
                "score": x["essay_score"] if essay else None,
                "feedback": {"good": " ".join(x["got_points"] or []), "miss": " ".join(x["missing_points"] or [])}
                if essay else None,
                "answer": x["correct_answer"], "explanation": x["explanation"],
                "evidence": {"materialId": src["material_id"], "page": src["start_page"], "quote": src["quote"]}
                if src else None,
            })
        hist = self.conn.execute(select(t.mastery_history).where(
            t.mastery_history.c.quiz_session_id == session["id"])).mappings().all()
        unit_changes, book_changes = [], []
        for h in hist:
            u = data.units[h["unit_id"]]
            no, title = unit_no(u["code"]), u["title"]
            unit_changes.append({"unitNo": no, "title": title, "before": h["previous_score"], "after": h["score"],
                                 "grade": h["level"], "note": "회복 모드" if h["recovery_mode"] else None})
            unit_tasks = [x for x in data.tasks if data.unit_of(x) and data.unit_of(x)["id"] == u["id"]
                          and x.row["status"] not in ("carried_over", "excluded")]
            after_blocks = [data.block_state(x, self.today) for x in unit_tasks]
            before_blocks = []
            for x, b in zip(unit_tasks, after_blocks):
                if b not in ("stable", "shaky", "risk", "perfect"):
                    before_blocks.append(b)  # 예정·진행 중은 그대로
                elif x.record and x.record["study_date"] == self.today:
                    before_blocks.append("done")  # 오늘 쌓은 회색 블록이 등급 색으로 (P3)
                else:
                    before_blocks.append(h["previous_level"] or "done")
            book_changes.append({"unitNo": no, "title": title,
                                 "before": {"score": h["previous_score"], "grade": h["previous_level"], "blocks": before_blocks},
                                 "after": {"score": h["score"], "grade": h["level"], "blocks": after_blocks},
                                 "recovering": bool(h["recovery_mode"])})
        summary = {"total": len(out_items), "correct": sum(1 for x in out_items if x["correct"]),
                   "sureCorrect": sum(1 for x in out_items if x["correct"] and x["confidence"] == "sure"),
                   "unsureCorrect": sum(1 for x in out_items if x["correct"] and x["confidence"] == "unsure"),
                   "wrong": sum(1 for x in out_items if not x["correct"])}
        subject = data.subject_by_id.get(session["subject_id"], {})
        result = {
            "quizId": session["id"], "subjectId": session["subject_id"], "name": subject.get("name"),
            "essayGrading": "saved" if session["is_fallback"] else "ai", "summary": summary, "items": out_items,
            "unitChanges": unit_changes, "bookChanges": book_changes,
            "wrongAdded": [{"no": x["no"], "reason": "애매 정답" if x["correct"] else "오답"}
                           for x in out_items if not x["correct"] or x["confidence"] == "unsure"],
        }
        if session["type"] == "extra":
            scope = self.conn.execute(select(t.quiz_scopes.c.unit_id).where(
                t.quiz_scopes.c.quiz_session_id == session["id"])).scalar()
            result["unitNo"] = unit_no(data.units[scope]["code"]) if scope in data.units else None
            result["moreUnitChange"] = ({"unitNo": result["unitNo"], "before": unit_changes[0]["before"],
                                         "after": unit_changes[0]["after"]} if unit_changes else None)
        else:
            extras = self.conn.execute(select(t.quiz_sessions).where(
                t.quiz_sessions.c.parent_quiz_session_id == session["id"],
                t.quiz_sessions.c.status == "submitted")).mappings().all()
            result["more"] = [self.result_view(dict(e)) for e in extras]
        return result
