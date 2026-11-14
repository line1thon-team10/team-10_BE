"""시드를 DB에 넣어요. 한 사용자의 데이터만 지우고 다시 넣어서 다른 사용자에게 영향이 없어요."""
import uuid
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, delete, select, update

from app.core.clock import now_utc
from app.db import schema as t
from app.domain.focus import quote_for
from app.seed import demo_data as d
from app.seed.package import load_package

KST = ZoneInfo("Asia/Seoul")


def _id() -> str:
    return str(uuid.uuid4())


def _at(day, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), KST).astimezone(timezone.utc)


# 사용자 데이터를 지우는 순서 (자식 → 부모). user_id가 직접 없는 표는 소속 ID로 지워요
def wipe_user(conn: Connection, user_id: str) -> None:
    subj = select(t.subjects.c.id).where(t.subjects.c.user_id == user_id)
    mats = select(t.materials.c.id).where(t.materials.c.subject_id.in_(subj))
    unit_ids = select(t.units.c.id).where(t.units.c.material_id.in_(mats))
    plan_ids = select(t.plans.c.id).where(t.plans.c.user_id == user_id)
    task_ids = select(t.study_tasks.c.id).where(t.study_tasks.c.subject_id.in_(subj))
    rec_ids = select(t.study_records.c.id).where(t.study_records.c.task_id.in_(task_ids))
    sess_ids = select(t.study_sessions.c.id).where(t.study_sessions.c.user_id == user_id)
    quiz_ids = select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.user_id == user_id)
    item_ids = select(t.quiz_items.c.id).where(t.quiz_items.c.quiz_session_id.in_(quiz_ids))
    q_ids = select(t.questions.c.id).where(t.questions.c.unit_id.in_(unit_ids))
    req_ids = select(t.plan_change_requests.c.id).where(t.plan_change_requests.c.user_id == user_id)
    msg_ids = select(t.tutor_messages.c.id).where(t.tutor_messages.c.user_id == user_id)
    job_ids = select(t.ai_jobs.c.id).where(t.ai_jobs.c.user_id == user_id)

    obligation_ids = select(t.remedial_obligations.c.id).where(t.remedial_obligations.c.user_id == user_id)

    steps = [
        delete(t.remedial_obligation_blocks).where(t.remedial_obligation_blocks.c.obligation_id.in_(obligation_ids)),
        delete(t.remedial_obligations).where(t.remedial_obligations.c.user_id == user_id),
        delete(t.ai_usage_logs).where(t.ai_usage_logs.c.user_id == user_id),
        delete(t.ai_jobs).where(t.ai_jobs.c.id.in_(job_ids)),
        delete(t.ai_saved_results).where(t.ai_saved_results.c.user_id == user_id),
        delete(t.reflection_reports).where(t.reflection_reports.c.user_id == user_id),
        delete(t.tutor_sources).where(t.tutor_sources.c.message_id.in_(msg_ids)),
        update(t.tutor_messages).where(t.tutor_messages.c.user_id == user_id).values(parent_message_id=None),
        delete(t.tutor_messages).where(t.tutor_messages.c.user_id == user_id),
        delete(t.record_requests).where(t.record_requests.c.user_id == user_id),
        delete(t.user_notices).where(t.user_notices.c.user_id == user_id),
        delete(t.weak_questions).where(t.weak_questions.c.question_id.in_(q_ids)),
        delete(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id.in_(item_ids)),
        update(t.quiz_items).where(t.quiz_items.c.quiz_session_id.in_(quiz_ids)).values(origin_quiz_item_id=None),
        delete(t.quiz_items).where(t.quiz_items.c.quiz_session_id.in_(quiz_ids)),
        delete(t.quiz_scopes).where(t.quiz_scopes.c.quiz_session_id.in_(quiz_ids)),
        delete(t.mastery_history).where(t.mastery_history.c.unit_id.in_(unit_ids)),
        update(t.quiz_sessions).where(t.quiz_sessions.c.user_id == user_id).values(parent_quiz_session_id=None),
        delete(t.quiz_sessions).where(t.quiz_sessions.c.user_id == user_id),
        delete(t.question_sources).where(t.question_sources.c.question_id.in_(q_ids)),
        delete(t.questions).where(t.questions.c.id.in_(q_ids)),
        delete(t.learning_passes).where(t.learning_passes.c.unit_id.in_(unit_ids)),
        delete(t.speed_calibrations).where(t.speed_calibrations.c.subject_id.in_(subj)),
        delete(t.study_record_ranges).where(t.study_record_ranges.c.record_id.in_(rec_ids)),
        delete(t.study_records).where(t.study_records.c.id.in_(rec_ids)),
        delete(t.study_session_segments).where(t.study_session_segments.c.session_id.in_(sess_ids)),
        delete(t.study_sessions).where(t.study_sessions.c.user_id == user_id),
        delete(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id == user_id),
        delete(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_(task_ids)),
        update(t.study_tasks).where(t.study_tasks.c.subject_id.in_(subj)).values(origin_task_id=None),
        delete(t.study_tasks).where(t.study_tasks.c.subject_id.in_(subj)),
        delete(t.study_blocks).where(t.study_blocks.c.subject_id.in_(subj)),
        delete(t.onboarding_messages).where(t.onboarding_messages.c.user_id == user_id),
        update(t.plan_change_requests).where(t.plan_change_requests.c.user_id == user_id)
        .values(base_proposal_id=None, accepted_proposal_id=None),
        delete(t.plan_proposals).where(t.plan_proposals.c.request_id.in_(req_ids)),
        delete(t.plan_change_requests).where(t.plan_change_requests.c.user_id == user_id),
        delete(t.plans).where(t.plans.c.id.in_(plan_ids)),
        delete(t.concepts).where(t.concepts.c.unit_id.in_(unit_ids)),
        delete(t.units).where(t.units.c.id.in_(unit_ids)),
        delete(t.materials).where(t.materials.c.id.in_(mats)),
        delete(t.calendar_event_exceptions).where(
            t.calendar_event_exceptions.c.event_id.in_(
                select(t.calendar_events.c.id).where(t.calendar_events.c.user_id == user_id))),
        delete(t.calendar_events).where(t.calendar_events.c.user_id == user_id),
        delete(t.subjects).where(t.subjects.c.user_id == user_id),
    ]
    for stmt in steps:
        conn.execute(stmt)


def load_seed(conn: Connection, user_id: str, seed: str) -> None:
    now = now_utc()
    wipe_user(conn, user_id)
    seed_day = d.SEED_DATES[seed]
    has_plan = seed != "empty"
    conn.execute(update(t.users).where(t.users.c.id == user_id).values(
        demo_date=seed_day, demo_seed=seed, last_assessed_date=seed_day - timedelta(days=1),
        onboarding_status="completed" if has_plan else "draft", updated_at=now))
    if not has_plan:
        return

    # 과목·자료·단원·개념
    subject_defs, quiz, mastery = load_package()
    subject_id, unit_id, unit_pages, concept_id, material_of = {}, {}, {}, {}, {}
    for s in subject_defs:
        sid = subject_id[s["code"]] = _id()
        conn.execute(t.subjects.insert().values(
            id=sid, user_id=user_id, code=s["code"], name=s["name"], color=s["color"], status="active",
            minutes_per_page=d.MIN_PER_PAGE, exam_name="기말고사", exam_date=s["exam_date"],
            raw_goal=s["raw_goal"], target_grade=s["target_grade"], target_score=s["target_score"],
            passing_criteria=s["passing_criteria"], grading_weights=s["grading_weights"],
            emphasized_content=s["emphasized_content"], goal_confirmed_at=now, created_at=now, updated_at=now))
        fname, pages = s["file"]
        mid = _id()
        conn.execute(t.materials.insert().values(
            id=mid, subject_id=sid, original_name=fname, source_format="pdf", sha256=s["sha256"],
            page_count=pages, extraction_method="text", analysis_version=d.PACKAGE_VERSION,
            analysis_status="succeeded", quiz_enabled=True, question_generation_status="ready",
            created_at=now, updated_at=now))
        for no, (title, start, end, diff, concepts) in enumerate(s["units"], start=1):
            code = f"{s['code']}{no}"
            uid = unit_id[code] = _id()
            unit_pages[code] = (start, end)
            score, level, recovery = mastery.get(code, (None, None, False))
            emphasis = 1.5 if s["emphasized_content"] and title.startswith("트리") else 1.0
            conn.execute(t.units.insert().values(
                id=uid, material_id=mid, code=code, title=title, sort_order=no, quantity_basis="page",
                start_page=start, end_page=end, difficulty=diff, in_scope=True, emphasis_weight=emphasis,
                evidence=[f"목차 {no}장 '{title}' (p.{start}–{end})"],
                mastery_score=score, mastery_level=level, recovery_mode=recovery,
                mastery_updated_at=now if score is not None else None, created_at=now, updated_at=now))
            material_of[code] = mid
            for ci, c in enumerate([c for c in quiz["concepts"] if c["unit"] == code]):
                cid = concept_id[c["id"]] = _id()
                conn.execute(t.concepts.insert().values(
                    id=cid, unit_id=uid, code=c["id"], title=c["name"], sort_order=ci + 1,
                    start_page=c["page_start"], end_page=c["page_end"], difficulty=diff, in_scope=True,
                    created_at=now, updated_at=now))

    # 고정 일정 (반복 규칙 1개로 저장)
    for kind, title, days, start, end in d.EVENTS:
        first = d.PLAN_START
        wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        targets = [wd[x] for x in days.split(",")]
        while first.weekday() not in targets:
            first += timedelta(days=1)
        conn.execute(t.calendar_events.insert().values(
            id=_id(), user_id=user_id, event_type=kind, title=title,
            starts_at=_at(first, start), ends_at=_at(first, end),
            recurrence_rule=f"FREQ=WEEKLY;BYDAY={days}", recurrence_until=d.EVENT_UNTIL,
            created_at=now, updated_at=now))

    # 계획 (사용자당 1개) · 블록 · 태스크 · 기록
    plan_id = _id()
    conn.execute(t.plans.insert().values(
        id=plan_id, user_id=user_id, revision=1, created_at=now,
        approved_at=datetime.combine(d.PLAN_START, time(21, 0), KST)))
    totals = defaultdict(lambda: [0, 0])  # (날짜, 과목) → [새 범위 p, 전체 p]
    completed_units = defaultdict(lambda: {"pages": set(), "records": [], "day": None})
    day9_index = 0
    focus_task_id = None
    focus_expected_seconds = None
    for day, scode, unit_no, ttype, ranges, minutes, reason in d.PLAN:
        if day < seed_day:
            done = True
        elif day == seed_day:
            done = day9_index in d.DONE_ON_DAY9[seed]
            day9_index += 1
        else:
            done = False
        sid = subject_id[scode]
        block_id, task_id = _id(), _id()
        unit_ref = unit_id[f"{scode}{unit_no}"] if unit_no else None
        conn.execute(t.study_blocks.insert().values(
            id=block_id, subject_id=sid, unit_id=unit_ref, task_type=ttype,
            stacked_at=datetime.combine(day, time(16, 0), KST) if done else None, created_at=now))
        title = {"review": "총복습", "remedial": "보충"}.get(ttype, "")
        if unit_no:
            utitle = subject_defs[[s["code"] for s in subject_defs].index(scode)]["units"][unit_no - 1][0]
            title = f"{unit_no}단원 {utitle}" + (" 보충" if ttype == "remedial" else "")
        conn.execute(t.study_tasks.insert().values(
            id=task_id, plan_id=plan_id, subject_id=sid, block_id=block_id, task_type=ttype, title=title,
            scheduled_date=day, estimated_minutes=minutes, placement_reason=reason,
            status="completed" if done else "planned", created_at=now))
        if seed == "day9" and day == seed_day and not done and focus_task_id is None:
            focus_task_id = task_id
            focus_expected_seconds = minutes * 60
        for i, (uno, sp, ep) in enumerate(ranges):
            conn.execute(t.study_task_ranges.insert().values(
                id=_id(), task_id=task_id, unit_id=unit_id[f"{scode}{uno}"], start_page=sp, end_page=ep,
                sort_order=i + 1))
            pages = ep - sp + 1
            totals[(day, scode)][1] += pages
            if ttype == "study":
                totals[(day, scode)][0] += pages
        if done:
            rec_id = _id()
            conn.execute(t.study_records.insert().values(
                id=rec_id, task_id=task_id, result="completed", recorded_at=datetime.combine(day, time(16, 0), KST),
                study_date=day, updated_at=now))
            for uno, sp, ep in ranges:
                evidence = completed_units[f"{scode}{uno}"]
                evidence["pages"].update(range(sp, ep + 1))
                evidence["records"].append(rec_id)
                evidence["day"] = day
                conn.execute(t.study_record_ranges.insert().values(
                    id=_id(), record_id=rec_id, unit_id=unit_id[f"{scode}{uno}"], start_page=sp, end_page=ep))

    for (day, scode), (new_p, total_p) in totals.items():
        conn.execute(t.daily_plan_totals.insert().values(
            user_id=user_id, subject_id=subject_id[scode], study_date=day, plan_id=plan_id,
            planned_new_pages=new_p, planned_total_pages=total_p, planned_unpaged_minutes=0,
            finalized_at=now if day < seed_day else None, updated_at=now))

    # 완료된 페이지와 기록 ID로 회독 근거를 구성한다. 오늘 완료한 DB2/OS2도 포함한다.
    for code, evidence in completed_units.items():
        start_page, end_page = unit_pages[code]
        if not set(range(start_page, end_page + 1)) <= evidence["pages"]:
            continue
        day = evidence["day"]
        conn.execute(t.learning_passes.insert().values(
            id=_id(), unit_id=unit_id[code], pass_number=1, completion_kind="initial",
            evidence_key="initial",
            evidence_snapshot={"schemaVersion": "1", "recordIds": evidence["records"],
                               "requiredRanges": [[start_page, end_page]]},
            completed_at=datetime.combine(day, time(16, 0), KST), completed_date=day))

    if focus_task_id is not None:
        # 실제 서버 시계로 42분을 되돌린다. 가상 학습 날짜와 측정 시각은 분리한다.
        session_id = _id()
        started_at = now - timedelta(minutes=42)
        conn.execute(t.study_sessions.insert().values(
            id=session_id, user_id=user_id, task_id=focus_task_id, status="running",
            started_at=started_at, study_date=seed_day, last_confirmed_elapsed_seconds=0,
            expected_seconds=focus_expected_seconds, quote=quote_for(session_id), created_at=now))
        conn.execute(t.study_session_segments.insert().values(
            id=_id(), session_id=session_id, sort_order=1, started_at=started_at))

    _load_quiz(conn, user_id, seed_day, quiz, subject_id, unit_id, concept_id, material_of, now)


def _load_quiz(conn, user_id, seed_day, quiz, subject_id, unit_id, concept_id, material_of, now) -> None:
    """패키지 문항·풀이 기록·취약 문항·오늘 퀴즈 구성"""
    question_id = {}
    for q in quiz["questions"]:
        qid = question_id[q["id"]] = _id()
        qtype = d.TYPE_MAP[q["type"]]
        choices = ([{"id": f"{q['id']}-{i + 1}", "text": c} for i, c in enumerate(q["choices"])]
                   if q.get("choices") else None)
        conn.execute(t.questions.insert().values(
            id=qid, unit_id=unit_id[q["unit"]], concept_id=concept_id.get(q["concept"]), code=q["id"],
            type=qtype, difficulty=d.DIFF_MAP.get(q["difficulty"], "low"), prompt=q["question"], choices=choices,
            correct_answer=q["answer"], explanation=q.get("explanation"),
            origin="seed" if q.get("source") == "daily" else "generated", generation_version=d.PACKAGE_VERSION,
            created_at=now))
        conn.execute(t.question_sources.insert().values(
            id=_id(), question_id=qid, material_id=material_of[q["unit"]],
            start_page=q["page"], end_page=q["page"], quote=q.get("quote")))

    subj_of_q = {q["id"]: q["unit"][:2] for q in quiz["questions"]}
    # 날짜·과목별 퀴즈 1개로 묶어서 풀이 기록을 넣어요
    sessions = defaultdict(list)
    for a in quiz["attempts"]:
        if a["date"] < seed_day.isoformat():
            sessions[(a["date"], subj_of_q[a["question"]])].append(a)
    latest = {}
    for (day_s, scode), attempts in sorted(sessions.items()):
        day = datetime.fromisoformat(day_s).date()
        at = datetime.combine(day, time(20, 0), KST)
        qsid = _id()
        conn.execute(t.quiz_sessions.insert().values(
            id=qsid, user_id=user_id, subject_id=subject_id[scode], type="daily", quiz_date=day,
            status="submitted", selected_difficulty="low", current_position=len(attempts),
            grading_status="completed", created_at=at, submitted_at=at, finalized_at=at, graded_at=at))
        for i, a in enumerate(attempts):
            item_id = _id()
            conn.execute(t.quiz_items.insert().values(
                id=item_id, quiz_session_id=qsid, question_id=question_id[a["question"]], sort_order=i + 1,
                inclusion_reason="new"))
            conn.execute(t.quiz_attempts.insert().values(
                id=_id(), quiz_item_id=item_id, answer=a["answer"], confidence=d.CONF_MAP[a["confidence"]],
                is_correct=a["correct"], grading_status="graded", essay_score=a.get("essay_score"),
                answered_at=at, answered_date=day, finalized_at=at, graded_at=at, mastery_applied_at=at))
            latest[a["question"]] = (a, day)

    # 마지막 풀이가 틀렸거나 애매였던 문항 = 취약 문항 (4-3)
    for code, (a, day) in latest.items():
        if not a["correct"] or a["confidence"] == "애매":
            conn.execute(t.weak_questions.insert().values(
                question_id=question_id[code], status="active", review_step=0,
                next_review_date=day + timedelta(days=1), last_weak_at=datetime.combine(day, time(20, 0), KST)))

    # 오늘 단원당 새 문제 5 + 취약 문항 (AI 6 · DB 8 · OS 6)
    if seed_day.isoformat() == "2026-11-09":
        studied_units = {code for codes in d.TODAY_UNITS.values() for code in codes}
        for scode, codes in quiz["pending_quizzes_1109"].items():
            qsid = _id()
            conn.execute(t.quiz_sessions.insert().values(
                id=qsid, user_id=user_id, subject_id=subject_id[scode], type="daily", quiz_date=seed_day,
                status="ready", selected_difficulty="low", current_position=0, grading_status="pending",
                created_at=now))
            for i, code in enumerate(codes):
                unit = next(q["unit"] for q in quiz["questions"] if q["id"] == code)
                conn.execute(t.quiz_items.insert().values(
                    id=_id(), quiz_session_id=qsid, question_id=question_id[code], sort_order=i + 1,
                    inclusion_reason="new" if unit in studied_units and code not in latest else "weak"))
