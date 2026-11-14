"""오늘의 퀴즈 (QZ) · 이해도 반영 (LV).

- 문항 구성 (4-3): 오늘 공부한 단원마다 새 문제 5 + 다시 볼 취약 문항 전부
- 채점: 객관식·OX·단답형은 서버, 서술형은 AI(85점 이상 정답). AI가 안 되면 저장된 채점으로 (NF-10)
- 단원 점수: 현민님 learning_metrics(calc.py와 같은 식)에 전체 풀이 기록을 넣어 다시 계산해요
"""
import re
import uuid
from copy import deepcopy
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import Connection, func, select, update

from app.ai import engine as ai
from app.ai.metrics_adapter import calculate_learning_metrics
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.quiz_answers import QuizAnswerRepository
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
    def session(self, key: str, quiz_id: str | None = None, difficulty: str | None = None) -> dict:
        """명시한 quiz_id는 새 세트를 만들지 않고 경로·소유 과목까지 검증한다."""
        owned_subjects = select(t.subjects.c.id).where(t.subjects.c.user_id == self.user["id"])
        s = self.conn.execute(select(t.quiz_sessions).where(
            t.quiz_sessions.c.id == (quiz_id if quiz_id is not None else key),
            t.quiz_sessions.c.user_id == self.user["id"],
            t.quiz_sessions.c.subject_id.is_(None) | t.quiz_sessions.c.subject_id.in_(owned_subjects)
        )).mappings().first()
        if s:
            if quiz_id is not None and key not in (s["id"], s["subject_id"]):
                raise ApiError("NO_QUIZ", "퀴즈를 찾지 못했어요", 404)
            if difficulty is not None and difficulty != s['selected_difficulty']:
                raise ApiError('QUIZ_DIFFICULTY_CONFLICT', '이미 준비된 퀴즈의 난이도는 바꿀 수 없어요', 409)
            return dict(s)
        if quiz_id is not None or key not in self.data.subject_by_id:
            raise ApiError("NO_QUIZ", "퀴즈를 찾지 못했어요", 404)
        return self.ensure_daily(key, difficulty)

    def daily_sessions(self, subject_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(select(t.quiz_sessions).where(
            t.quiz_sessions.c.user_id == self.user["id"], t.quiz_sessions.c.subject_id == subject_id,
            t.quiz_sessions.c.quiz_date == self.today, t.quiz_sessions.c.type == "daily")
            .order_by(t.quiz_sessions.c.created_at)).mappings()]

    def result_session(self, key: str, quiz_id: str | None = None) -> dict:
        """Result reads never generate another daily set."""
        if quiz_id is not None:
            return self.session(key, quiz_id=quiz_id)
        direct = self.conn.scalar(select(t.quiz_sessions.c.id).where(
            t.quiz_sessions.c.id == key, t.quiz_sessions.c.user_id == self.user["id"]))
        if direct:
            return QuizAnswerRepository(self.conn).quiz(self.user["id"], direct)
        subject = self.conn.scalar(select(t.subjects.c.id).where(
            t.subjects.c.id == key, t.subjects.c.user_id == self.user["id"]))
        if subject is None:
            raise ApiError("NO_QUIZ", "퀴즈를 찾지 못했어요", 404)
        latest = self.conn.scalar(select(t.quiz_sessions.c.id).where(
            t.quiz_sessions.c.user_id == self.user["id"], t.quiz_sessions.c.subject_id == subject,
            t.quiz_sessions.c.type == "daily").order_by(
                t.quiz_sessions.c.quiz_date.desc(), t.quiz_sessions.c.created_at.desc(), t.quiz_sessions.c.id.desc()))
        if latest is None:
            raise ApiError("NO_RESULT", "아직 제출한 퀴즈가 없어요", 404)
        return QuizAnswerRepository(self.conn).quiz(self.user["id"], latest)

    def items(self, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            select(t.quiz_items.c.id.label("item_id"), t.quiz_items.c.question_id,
                   t.quiz_items.c.sort_order, t.quiz_items.c.inclusion_reason,
                   *(column for column in t.questions.c if column.name != "id"),
                   t.units.c.material_id,
                   t.quiz_attempts.c.id.label("attempt_id"), t.quiz_attempts.c.answer,
                   t.quiz_attempts.c.confidence, t.quiz_attempts.c.is_correct, t.quiz_attempts.c.essay_score,
                   t.quiz_attempts.c.got_points, t.quiz_attempts.c.missing_points,
                   t.quiz_attempts.c.grading_status)
            .join(t.questions, t.quiz_items.c.question_id == t.questions.c.id)
            .join(t.units, t.questions.c.unit_id == t.units.c.id)
            .outerjoin(t.quiz_attempts, t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id)
            .where(t.quiz_items.c.quiz_session_id == session_id).order_by(t.quiz_items.c.sort_order)).mappings()
        out = [dict(r) for r in rows]
        if out:
            srcs = {}
            for r in self.conn.execute(select(t.question_sources).where(
                    t.question_sources.c.question_id.in_([x["question_id"] for x in out])).order_by(t.question_sources.c.id)).mappings():
                srcs.setdefault(r["question_id"], dict(r))
            for x in out:
                x["source"] = srcs.get(x["question_id"])
        return out

    # ---------- 구성 ----------
    def ensure_daily(self, subject_id: str, difficulty: str | None = None) -> dict:
        from app.services.quiz_selection import QuizSelection
        return QuizSelection(self.conn, self.user, self.today).ensure_daily(subject_id, difficulty)

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
        rounds = dict(self.conn.execute(select(t.learning_passes.c.unit_id, func.count(t.learning_passes.c.id)).where(
            t.learning_passes.c.unit_id.in_(list(unit_ids)), t.learning_passes.c.invalidated_at.is_(None))
            .group_by(t.learning_passes.c.unit_id)).all())
        return f"{max(1, min((rounds.get(uid, 0) for uid in unit_ids), default=0))}회독"

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
        return {"id": x["item_id"], "type": qtype, "tag": TAGS.get(x["inclusion_reason"]),
                "unitNo": unit_no(u["code"]) if u else None,
                "materialId": x["material_id"], "unitId": x["unit_id"],
                "prompt": x["prompt"],
                "options": [c["text"] for c in x["choices"]] if x["choices"] else None}

    def set_view(self, session: dict, saved: bool = False) -> dict:
        # list_current validates the set's material ownership even when no
        # answers exist. Never infer legacy choice indices from answer text.
        saved_answers = [{"questionId": answer["quiz_item_id"], "choice": answer["choice"],
                          "text": answer["text"], "confidence": answer["confidence"],
                          "revision": answer["revision"]}
                         for answer in QuizAnswerRepository(self.conn).list_current(self.user["id"], session["id"])
                         if answer["input_recovered"]]
        items = self.items(session["id"])
        subject = self.data.subject_by_id.get(session["subject_id"], {})
        comp, _ = self.composition(items)
        return {
            "quizId": session["id"],
            "savedAnswers": saved_answers,
            "saved": saved or session["is_fallback"], "subjectId": session["subject_id"] if session["type"] == "daily"
            else session["id"], "custom": session["type"] != "daily",
            "resultFor": session["subject_id"] if session["type"] != "daily" else session["subject_id"],
            "status": session["status"], "name": subject.get("name"),
            "round": self.round_of(items), "difficulty": DIFF_KO.get(session["selected_difficulty"], "하"),
            "composition": comp, "questions": [self.question_view(x) for x in items],
        }

    # ---------- 채점 ----------
    def _validated_answers(self, items: list[dict], answers: list[dict]) -> dict[str, dict]:
        """전체 답안의 항목·타입을 확인한 뒤에만 채점과 저장을 시작한다."""
        by_item = {answer["questionId"]: answer for answer in answers}
        if len(by_item) != len(answers) or set(by_item) != {item["item_id"] for item in items}:
            raise ApiError("INVALID_INPUT", "현재 퀴즈의 모든 문항을 정확히 한 번씩 제출해 주세요", 422)
        if any(answer.get("confidence") is None for answer in answers):
            raise ApiError("CONFIDENCE_REQUIRED", "모든 문항에 확실/애매를 골라 주세요")
        for item in items:
            answer = by_item[item["item_id"]]
            if answer["confidence"] not in CONF_KO:
                raise ApiError("INVALID_INPUT", "확신도는 확실/애매 중에서 골라 주세요", 422)
            if item["type"] in ("choice", "ox"):
                choice = answer.get("choice")
                if (type(choice) is not int or not 0 <= choice < len(item["choices"] or [])
                        or answer.get("text") is not None):
                    raise ApiError("INVALID_INPUT", "객관식 답은 유효한 보기 번호로 제출해 주세요", 422)
            elif not isinstance(answer.get("text"), str) or answer.get("choice") is not None:
                raise ApiError("INVALID_INPUT", "단답·서술형 답은 문자열로 제출해 주세요", 422)
        return by_item

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
        elif (w and w["status"] == "active" and w["next_review_date"] is not None
              and self.today >= w["next_review_date"]):
            step = w["review_step"] + 1
            values = (dict(status="resolved", review_step=2, next_review_date=None, resolved_at=now) if step >= 2 else
                      dict(review_step=step, next_review_date=self.today.fromordinal(self.today.toordinal() + 3)))
            self.conn.execute(update(t.weak_questions).where(t.weak_questions.c.question_id == question_id).values(**values))

    def _metrics_input(self) -> dict:
        """Owned UUID snapshots; only complete finalized sets contribute facts."""
        units = self.conn.execute(select(t.units, t.materials.c.subject_id,
            t.materials.c.sha256.label("material_sha256"), t.materials.c.analysis_version)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .where(t.units.c.id.in_(list(self.data.units)))).mappings().all()
        unit_ids = [unit["id"] for unit in units]
        concepts = self.conn.execute(select(t.concepts).where(t.concepts.c.unit_id.in_(unit_ids))).mappings().all()
        questions = self.conn.execute(select(t.questions.c.id, t.questions.c.unit_id, t.questions.c.concept_id,
            t.questions.c.code, t.questions.c.difficulty).where(t.questions.c.unit_id.in_(unit_ids))).mappings().all()
        attempts = self.conn.execute(
            select(t.quiz_attempts.c.id, t.quiz_attempts.c.quiz_item_id, t.quiz_attempts.c.answered_date,
                   t.quiz_attempts.c.answered_at, t.quiz_attempts.c.finalized_at, t.quiz_attempts.c.graded_at,
                   t.quiz_attempts.c.grading_status, t.quiz_attempts.c.confidence, t.quiz_attempts.c.is_correct,
                   t.quiz_items.c.question_id, t.quiz_sessions.c.type.label("quiz_type"),
                   t.quiz_sessions.c.status.label("session_status"),
                   t.quiz_sessions.c.finalized_at.label("session_finalized_at"),
                   t.quiz_sessions.c.grading_status.label("session_grading_status"))
            .join(t.quiz_items, t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id)
            .join(t.quiz_sessions, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .join(t.questions, t.quiz_items.c.question_id == t.questions.c.id)
            .join(t.units, t.questions.c.unit_id == t.units.c.id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .where(t.questions.c.unit_id.in_(unit_ids), t.quiz_sessions.c.user_id == self.user["id"],
                   t.quiz_sessions.c.subject_id.is_(None) | (t.quiz_sessions.c.subject_id == t.materials.c.subject_id),
                   t.quiz_sessions.c.status == "submitted", t.quiz_sessions.c.finalized_at.is_not(None),
                   t.quiz_sessions.c.grading_status == "completed", t.quiz_attempts.c.finalized_at.is_not(None),
                   t.quiz_attempts.c.graded_at.is_not(None), t.quiz_attempts.c.grading_status.in_(("graded", "fallback")),
                   t.quiz_attempts.c.is_correct.is_not(None), t.quiz_attempts.c.answered_date <= self.today)
            .order_by(t.quiz_attempts.c.answered_date, t.quiz_attempts.c.answered_at, t.quiz_attempts.c.id)).mappings().all()
        entries = self.conn.execute(select(t.mastery_history)
            .join(t.quiz_sessions, t.mastery_history.c.quiz_session_id == t.quiz_sessions.c.id)
            .where(t.mastery_history.c.unit_id.in_(unit_ids), t.mastery_history.c.score < 80,
                   t.mastery_history.c.recorded_date <= self.today, t.quiz_sessions.c.user_id == self.user["id"],
                   t.quiz_sessions.c.status == "submitted", t.quiz_sessions.c.finalized_at.is_not(None),
                   t.quiz_sessions.c.grading_status == "completed")
            .order_by(t.mastery_history.c.recorded_date, t.mastery_history.c.created_at, t.mastery_history.c.id)).mappings().all()
        # S-03 will supply verified current-scope eligibility. Legacy unit flags,
        # arbitrary pass rows, and repeated submissions do not establish it.
        return {"units": units, "concepts": concepts, "questions": questions, "attempts": attempts, "recovery_entries": entries}

    def _recalc_mastery(self, quiz_session_id: str, unit_ids: set[str]) -> None:
        finalized = self.conn.scalar(select(t.quiz_sessions.c.id).where(
            t.quiz_sessions.c.id == quiz_session_id, t.quiz_sessions.c.user_id == self.user["id"],
            t.quiz_sessions.c.status == "submitted", t.quiz_sessions.c.finalized_at.is_not(None),
            t.quiz_sessions.c.grading_status == "completed"))
        if finalized is None:
            raise ValueError("Mastery requires a finalized owned quiz set")
        recorded = set(self.conn.scalars(select(t.mastery_history.c.unit_id).where(t.mastery_history.c.quiz_session_id == quiz_session_id)))
        pending = unit_ids - recorded
        if not pending:
            return
        result = calculate_learning_metrics(self._metrics_input())
        by_unit = {row.unit_id: row for row in result.units}
        now = _now()
        for uid in sorted(pending):
            r = by_unit.get(uid)
            if not r:
                continue
            u = self.conn.execute(select(t.units).where(t.units.c.id == uid)).mappings().one()
            self.conn.execute(update(t.units).where(t.units.c.id == uid).values(
                mastery_score=r.score, mastery_level=r.grade, recovery_mode=r.recovery_mode,
                recovery_started_at=u["recovery_started_at"] or (now if r.recovery_mode else None),
                mastery_updated_at=now, updated_at=now))
            self.conn.execute(t.mastery_history.insert().values(
                id=str(uuid.uuid4()), unit_id=uid, quiz_session_id=quiz_session_id,
                previous_score=u["mastery_score"], previous_level=u["mastery_level"], raw_score=float(r.raw_score),
                score=r.score, level=r.grade, recovery_mode=r.recovery_mode, recorded_date=self.today,
                daily_increase_limit=r.daily_increase_limit, created_at=now))

    # ---------- 결과 ----------
    def _result_evidence(self, source):
        if source is None:
            return None
        material = self.conn.execute(select(t.materials).join(t.subjects,
            t.materials.c.subject_id == t.subjects.c.id).where(t.materials.c.id == source["material_id"],
                t.subjects.c.user_id == self.user["id"])).mappings().first()
        start, end, quote = source["start_page"], source["end_page"], source["quote"]
        if (material is None or type(material["page_count"]) is not int
                or type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= material["page_count"] or type(quote) is not str or not quote.strip()):
            return None
        # No writer/version currently establishes the shape of highlight JSON.
        # Keep verified citation navigation without inventing rectangle geometry.
        return {"materialId": material["id"], "page": start, "quote": quote, "rects": []}

    def result_view(self, session: dict, *, seen=frozenset()) -> dict:
        repo = QuizAnswerRepository(self.conn)
        repo.items(self.user["id"], session["id"])
        state = repo.submission(self.user["id"], session["id"])
        if state is not None and state["result_snapshot"] is not None:
            result = deepcopy(state["result_snapshot"])
        else:
            if state is not None and not state["legacy"]:
                raise ApiError("AI_FAILED", "채점이 아직 끝나지 않았어요", 503)
            if session["status"] != "submitted":
                raise ApiError("NO_RESULT", "아직 제출한 퀴즈가 없어요", 404)
            from app.services.quiz_finalization import legacy_result
            result = legacy_result(self.conn, self.user["id"], session["id"], seen=seen)
        if session["type"] != "extra":
            result["more"] = self._completed_more(session, seen=seen)
        return result

    def _completed_more(self, session: dict, *, seen=frozenset()) -> list[dict]:
        # Compose only published child snapshots on a copy. POST retries keep
        # replaying the original snapshot even when GET later has more results.
        extras = self.conn.execute(select(t.quiz_sessions).join(t.quiz_submission_states,
            t.quiz_submission_states.c.quiz_session_id == t.quiz_sessions.c.id).where(
            t.quiz_sessions.c.parent_quiz_session_id == session["id"],
            t.quiz_sessions.c.user_id == self.user["id"],
            t.quiz_sessions.c.status == "submitted", t.quiz_sessions.c.grading_status == "completed",
            t.quiz_sessions.c.finalized_at.is_not(None),
            t.quiz_submission_states.c.result_snapshot.is_not(None)).order_by(
                t.quiz_sessions.c.created_at, t.quiz_sessions.c.id)).mappings().all()
        visited = seen | {session["id"]}
        return [self.result_view(dict(extra), seen=visited) for extra in extras if extra["id"] not in visited]

    def _build_result_view(self, session: dict, *, before_blocks: dict | None = None, seen=frozenset()) -> dict:
        if session["status"] != "submitted":
            raise ApiError("NO_RESULT", "아직 푼 퀴즈가 없어요", 404)
        data = StudyData(self.conn, self.user["id"])  # 채점 뒤 점수로 다시 읽어요
        # Archived/retired objects still identify historical result facts.
        units = {row["id"]: dict(row) for row in self.conn.execute(select(t.units)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
            .where(t.subjects.c.user_id == self.user["id"])).mappings()}
        items = self.items(session["id"])
        out_items = []
        for i, x in enumerate(items):
            src = x.get("source")
            evidence = self._result_evidence(src)
            essay = x["type"] == "essay"
            out_items.append({
                "questionId": x["item_id"], "no": i + 1, "unitNo": unit_no(units[x["unit_id"]]["code"]),
                "materialId": x["material_id"], "unitId": x["unit_id"],
                "type": "choice" if x["type"] in ("choice", "ox") else x["type"], "tag": TAGS.get(x["inclusion_reason"]),
                "prompt": x["prompt"], "page": f"p.{evidence['page']}" if evidence else None,
                "confidence": x["confidence"], "correct": bool(x["is_correct"]),
                "score": x["essay_score"] if essay else None,
                "feedback": {"good": " ".join(x["got_points"] or []), "miss": " ".join(x["missing_points"] or [])}
                if essay else None,
                "answer": x["correct_answer"], "explanation": x["explanation"],
                "evidence": evidence,
            })
        hist = self.conn.execute(select(t.mastery_history).where(
            t.mastery_history.c.quiz_session_id == session["id"])).mappings().all()
        unit_changes, book_changes = [], []
        for h in hist:
            if h["unit_id"] not in units:
                raise ApiError("NO_RESULT", "결과 단원의 소유권을 확인하지 못했어요", 404)
            u = units[h["unit_id"]]
            no, title = unit_no(u["code"]), u["title"]
            unit_changes.append({"materialId": u["material_id"], "unitId": u["id"],
                                 "unitNo": no, "title": title, "before": h["previous_score"], "after": h["score"],
                                 "grade": h["level"], "note": "회복 모드" if h["recovery_mode"] else None})
            unit_tasks = [x for x in data.tasks if data.unit_of(x) and data.unit_of(x)["id"] == u["id"]
                          and x.row["status"] not in ("carried_over", "excluded")]
            after_blocks = [data.block_state(x, self.today) for x in unit_tasks]
            previous_blocks = []
            for x, b in zip(unit_tasks, after_blocks):
                if b not in ("stable", "shaky", "risk", "perfect"):
                    previous_blocks.append(b)  # 예정·진행 중은 그대로
                elif x.record and x.record["study_date"] == self.today:
                    previous_blocks.append("done")  # 오늘 쌓은 회색 블록이 등급 색으로 (P3)
                else:
                    previous_blocks.append(h["previous_level"] or "done")
            if before_blocks is not None:
                previous_blocks = before_blocks.get(u["id"], [])
            book_changes.append({"materialId": u["material_id"], "unitId": u["id"], "unitNo": no, "title": title,
                                 "before": {"score": h["previous_score"], "grade": h["previous_level"], "blocks": previous_blocks},
                                 "after": {"score": h["score"], "grade": h["level"], "blocks": after_blocks},
                                 "recovering": bool(h["recovery_mode"])})
        summary = {"total": len(out_items), "correct": sum(1 for x in out_items if x["correct"]),
                   "sureCorrect": sum(1 for x in out_items if x["correct"] and x["confidence"] == "sure"),
                   "unsureCorrect": sum(1 for x in out_items if x["correct"] and x["confidence"] == "unsure"),
                   "wrong": sum(1 for x in out_items if not x["correct"])}
        subject = self.conn.execute(select(t.subjects).where(t.subjects.c.id == session["subject_id"],
            t.subjects.c.user_id == self.user["id"])).mappings().first() or {}
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
            result["unitId"] = scope
            result["materialId"] = units[scope]["material_id"] if scope in units else None
            result["unitNo"] = unit_no(units[scope]["code"]) if scope in units else None
            result["moreUnitChange"] = ({"materialId": result["materialId"], "unitId": scope,
                                         "unitNo": result["unitNo"], "before": unit_changes[0]["before"],
                                         "after": unit_changes[0]["after"]} if unit_changes else None)
        else:
            result["more"] = self._completed_more(session, seen=seen)
        return result
