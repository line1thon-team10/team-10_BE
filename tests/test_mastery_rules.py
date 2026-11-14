from dataclasses import FrozenInstanceError, replace
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.domain.mastery import (
    ConfirmedAttempt, DayEligibility, RecoveryEntry, UnitScope, calculate_mastery,
    grade, half_up, ordered_attempts, weighted_score,
)
from app.ai.metrics_adapter import calculate_learning_metrics, current_scope
from app.db import schema as t
from app.domain.user import User
from app.repositories.users import UserRepository
from app.services.quiz import QuizService


def uid(number):
    return str(UUID(int=number))


DAY = date(2026, 11, 9)
AT = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def fact(number, *, day=0, correct=True, confidence="sure", difficulty="low", **changes):
    fields = dict(attempt_id=uid(1000 + number), quiz_item_id=uid(2000 + number), question_id=uid(3000 + number),
                  material_id=uid(1), unit_id=uid(2), concept_id=uid(3), answered_date=DAY + timedelta(days=day),
                  answered_at=AT + timedelta(days=day, seconds=number), finalized_at=AT + timedelta(days=day, seconds=number + 1),
                  difficulty=difficulty, confidence=confidence, correct=correct, quiz_kind="daily")
    fields.update(changes)
    return ConfirmedAttempt(**fields)


def test_no_facts_is_different_from_a_confirmed_zero_and_facts_are_immutable():
    assert weighted_score([]) is None
    attempt = fact(1, correct=False)
    assert weighted_score([attempt]).value == 0
    with pytest.raises(FrozenInstanceError):
        attempt.correct = True


@pytest.mark.parametrize("confidence,correct,expected", [("sure", True, 100), ("unsure", True, 50),
                                                        ("sure", False, 0), ("unsure", False, 0)])
def test_confidence_and_correctness_points(confidence, correct, expected):
    assert weighted_score([fact(1, confidence=confidence, correct=correct)]).value == expected


def test_difficulty_is_an_exact_weight_without_rounding_intermediate_rates():
    score = weighted_score([fact(1, difficulty="low"), fact(2, difficulty="mid", confidence="unsure"),
                            fact(3, difficulty="high", correct=False)])
    assert score.all_rate == score.recent_rate == Fraction(7, 18)
    assert score.value == Fraction(350, 9) and half_up(score.value) == 39


@pytest.mark.parametrize("count,alpha", [(9, Fraction(1, 2)), (10, Fraction(2, 5)), (19, Fraction(2, 5)),
                                        (20, Fraction(3, 10)), (21, Fraction(3, 10))])
def test_recent_window_and_alpha_boundaries(count, alpha):
    score = weighted_score([fact(n, correct=n > 1) for n in range(1, count + 1)])
    assert score.total_count == count and score.recent_count == min(count, 20) and score.alpha == alpha
    all_rate = Fraction(count - 1, count)
    recent_rate = Fraction(1) if count == 21 else all_rate
    assert score.value == 100 * (alpha * all_rate + (1 - alpha) * recent_rate)


@pytest.mark.parametrize("value,rounded,level", [(Fraction(49), 49, "risk"), (Fraction(99, 2), 50, "shaky"),
    (Fraction(50), 50, "shaky"), (Fraction(79), 79, "shaky"), (Fraction(159, 2), 80, "stable"),
    (Fraction(80), 80, "stable"), (Fraction(99), 99, "stable")])
def test_half_up_and_grade_thresholds(value, rounded, level):
    assert half_up(value) == rounded and grade(rounded) == level


def test_exact_fraction_does_not_round_an_under_half_value_up():
    assert half_up(Fraction(50) - Fraction(1, 2) - Fraction(1, 10 ** 50)) == 49
    assert half_up(Fraction(99, 2)) == 50


def test_attempt_order_uses_date_instant_then_id_and_deduplicates():
    first, second = fact(1), fact(2)
    same_instant = replace(second, answered_at=first.answered_at, finalized_at=first.finalized_at)
    # answered_date is authoritative, including virtual demo dates.
    earlier_date = replace(fact(3), answered_date=DAY - timedelta(days=1))
    assert ordered_attempts([same_instant, first, earlier_date, first]) == (earlier_date, first, same_instant)
    assert weighted_score([first, first]) == weighted_score([first])
    shifted = replace(first, answered_at=first.answered_at.astimezone(timezone(timedelta(hours=9))))
    assert shifted == first


def test_conflicting_attempt_item_question_or_material_identity_is_rejected():
    first = fact(1)
    conflicts = [replace(first, correct=False), replace(fact(2), quiz_item_id=first.quiz_item_id),
                 replace(fact(2), question_id=first.question_id, concept_id=None), replace(fact(2), material_id=uid(50))]
    for second in conflicts:
        with pytest.raises(ValueError):
            ordered_attempts([first, second])
    with pytest.raises(ValueError):
        weighted_score([first, replace(fact(2), unit_id=uid(50))])


@pytest.mark.parametrize("changes", [{"correct": None}, {"correct": 1}, {"confidence": "확실"}, {"difficulty": "unknown"},
    {"quiz_kind": "ready"}, {"finalized_at": None}, {"answered_at": AT.replace(tzinfo=None)},
    {"finalized_at": AT - timedelta(days=1)}, {"answered_date": AT}, {"unit_id": "same-code"}, {"concept_id": ""}])
def test_unconfirmed_invalid_or_ambiguous_facts_are_rejected(changes):
    with pytest.raises(ValueError):
        fact(1, **changes)


SCOPE = UnitScope(uid(1), uid(2), "scope-v1")


def batch(day, correct_count=20, *, offset=None, kind="daily"):
    start = day * 100 if offset is None else offset
    return [fact(start + n, day=day, correct=n <= correct_count, quiz_kind=kind) for n in range(1, 21)]


def entry(score=70, *, day=0, number=1, **changes):
    fields = dict(material_id=uid(1), unit_id=uid(2), recorded_date=DAY + timedelta(days=day),
                  displayed_score=score, history_id=uid(8000 + number), recorded_at=AT + timedelta(days=day, seconds=1000 + number))
    fields.update(changes)
    return RecoveryEntry(**fields)


def calculate(facts, **kwargs):
    return calculate_mastery(facts, current_scopes=[SCOPE], **kwargs)


def test_first_measurement_is_not_anchored_to_zero_and_raw_remains_uncapped():
    result = calculate(batch(0))
    row = result.latest(uid(2))
    assert (row.raw_score, row.capped_score, row.score, row.grade) == (100, 99, 99, "stable")
    assert not row.recovery_mode and row.previous_score is None
    assert row.daily_increase_limit is None
    assert calculate([]).history == () and calculate([]).units == ()


def test_recovery_groups_each_date_once_and_does_not_stack_five_point_increases():
    facts = batch(0, 14) + batch(1) + batch(1, offset=200)
    result = calculate(facts)
    assert [row.score for row in result.history] == [70, 75]
    assert result.units[0].raw_score > 95 and result.units[0].daily_increase_limit == 5
    assert calculate(list(reversed(facts)) + [facts[0]]) == result
    assert calculate(batch(0, 14) + batch(1)).units[0].score == 75


def test_observed_first_day_entry_survives_later_high_aggregate_and_anchors_at_its_score():
    facts = batch(0, 14) + batch(0, offset=20) + batch(0, offset=40)
    result = calculate(facts, recovery_entries=[entry()]).units[0]
    assert result.raw_score == 97 and result.score == 75 and result.recovery_mode
    assert result.recovery_result_dates == (DAY,)
    assert result.recovery_started_date == DAY
    # Without an actual entry, do not invent an earlier low set from a question prefix.
    unobserved = calculate(facts).units[0]
    assert unobserved.score == 97 and not unobserved.recovery_mode


def test_first_day_fifteen_bonus_still_cannot_skip_the_two_date_stable_requirement():
    facts = batch(0, 14) + batch(0, offset=20, kind="custom") + batch(0, offset=40)
    verified = DayEligibility(SCOPE, DAY, full_relearned=True, custom_confirmed=True, supplement_completed=True)
    row = calculate(facts, recovery_entries=[entry()], eligibility=[verified]).units[0]
    assert row.daily_increase_limit == 15 and row.score == 79
    assert row.recovery_result_dates == (DAY,)


@pytest.mark.parametrize("full,custom,kind,expected", [(False, False, "custom", 75), (True, False, "custom", 75),
    (False, True, "custom", 75), (True, True, "daily", 75), (True, True, "custom", 85)])
def test_fifteen_requires_full_relearning_and_confirmed_custom_in_the_same_scope_and_date(full, custom, kind, expected):
    evidence = DayEligibility(SCOPE, DAY + timedelta(days=1), full_relearned=full, custom_confirmed=custom, supplement_completed=True)
    row = calculate(batch(0, 14) + batch(1, kind=kind), eligibility=[evidence]).units[0]
    assert row.score == expected and row.daily_increase_limit == (15 if expected == 85 else 5)


@pytest.mark.parametrize("mismatch", ["day", "revision", "unit", "material"])
def test_other_date_revision_unit_or_material_evidence_never_grants_a_bonus(mismatch):
    evidence = DayEligibility(SCOPE, DAY + timedelta(days=1), True, True, True)
    if mismatch == "day":
        evidence = replace(evidence, day=DAY)
    else:
        scope = replace(SCOPE, **{ {"revision": "revision", "unit": "unit_id", "material": "material_id"}[mismatch]:
                                 "old" if mismatch == "revision" else uid(99)})
        evidence = replace(evidence, scope=scope)
    row = calculate(batch(0, 14) + batch(1, kind="custom"), eligibility=[evidence]).units[0]
    assert row.score == 75 and row.daily_increase_limit == 5


def test_recovery_entry_date_counts_and_prior_dates_do_not_count_toward_two_results():
    facts = batch(0) + batch(1) + batch(2)
    recovery = entry(score=79, day=1)
    done = DayEligibility(SCOPE, DAY + timedelta(days=1), supplement_completed=True)
    result = calculate(facts, recovery_entries=[recovery], eligibility=[done])
    assert [row.score for row in result.history] == [99, 79, 84]
    assert result.history[1].recovery_result_dates == (DAY + timedelta(days=1),)
    assert result.history[2].recovery_result_dates == (DAY + timedelta(days=1), DAY + timedelta(days=2))
    assert result.history[2].grade == "stable" and result.history[2].recovery_mode


def test_seventy_nine_entry_and_next_day_result_with_supplement_can_recover():
    facts = [fact(1, difficulty="high"), fact(2, difficulty="mid", confidence="unsure")] + batch(1)
    result = calculate(facts, recovery_entries=[entry(79)],
                       eligibility=[DayEligibility(SCOPE, DAY + timedelta(days=1), supplement_completed=True)])
    assert [row.score for row in result.history] == [79, 84]
    assert result.history[0].capped_score == Fraction(550, 7)
    assert result.history[1].capped_score == Fraction(585, 7)  # prior unrounded value + 5


def test_many_result_dates_without_verified_supplement_never_promote_recovery_to_stable():
    facts = batch(0, 14) + batch(1) + batch(2) + batch(3)
    result = calculate(facts)
    assert [row.score for row in result.history] == [70, 75, 79, 79]
    assert result.units[0].raw_score > 90 and result.units[0].grade == "shaky"


def test_a_supplement_before_recovery_entry_is_not_current_recovery_evidence():
    row = calculate(batch(0) + batch(1) + batch(2), recovery_entries=[entry(79, day=1)],
                    eligibility=[DayEligibility(SCOPE, DAY, supplement_completed=True)]).units[0]
    assert row.score == 79


def test_decreases_are_unrestricted_and_risk_cannot_jump_to_stable_in_a_day():
    facts = batch(0) + batch(1, 0)
    falling = calculate(facts)
    assert [row.score for row in falling.history] == [99, 15]
    evidence = DayEligibility(SCOPE, DAY + timedelta(days=2), True, True, True)
    recovered = calculate(facts + batch(2, kind="custom"), eligibility=[evidence]).units[0]
    assert recovered.raw_score > 80 and recovered.score == 30 and recovered.grade == "risk"


def test_history_entry_selection_is_stable_and_conflicting_history_ids_are_rejected():
    first, later = entry(70), entry(75, number=2)
    facts = batch(0)
    assert calculate(facts, recovery_entries=[later, first, first]) == calculate(facts, recovery_entries=[first, later])
    assert calculate(facts, recovery_entries=[later, first]).units[0].score == 75
    with pytest.raises(ValueError):
        calculate(facts, recovery_entries=[first, replace(first, displayed_score=65)])
    for invalid in (True, -1, 80, 79.1):
        with pytest.raises(ValueError):
            entry(invalid)


def test_conceptless_and_real_concept_errors_stay_separate_per_material_and_unit():
    facts = [fact(1, correct=False, concept_id=None), fact(2, correct=False), fact(3, correct=False),
             fact(4, correct=False, concept_id=None, material_id=uid(4), unit_id=uid(5))]
    result = calculate_mastery(facts)
    assert len(result.units) == 2
    assert [(row.unit_id, row.concept_id, row.wrong_count, row.is_weak) for row in result.concept_errors] == [
        (uid(2), None, 1, False), (uid(2), uid(3), 2, True), (uid(5), None, 1, False)]


def adapter_input():
    units = [{"id": uid(n), "material_id": uid(n + 1), "code": "UNIT1", "material_sha256": "a" * 64,
              "analysis_version": "v1", "quantity_basis": "page", "start_page": 1, "end_page": 10,
              "in_scope": True, "updated_at": AT} for n in (10, 20)]
    concepts = [{"id": uid(n + 2), "unit_id": uid(n), "code": "CONCEPT1", "start_page": 1, "end_page": 2,
                 "in_scope": True, "retired_at": None} for n in (10, 20)]
    questions = [{"id": uid(30), "unit_id": uid(10), "concept_id": None, "code": "Q1", "difficulty": "low"},
                 {"id": uid(31), "unit_id": uid(10), "concept_id": uid(12), "code": "Q2", "difficulty": "low"},
                 {"id": uid(32), "unit_id": uid(20), "concept_id": uid(22), "code": "Q1", "difficulty": "low"}]
    attempts = [{"id": uid(100 + i), "quiz_item_id": uid(200 + i), "question_id": question["id"],
                 "answered_date": DAY, "answered_at": AT, "finalized_at": AT, "graded_at": AT,
                 "grading_status": "graded", "session_status": "submitted", "session_finalized_at": AT,
                 "session_grading_status": "completed", "quiz_type": "practice", "confidence": "sure",
                 "is_correct": i == 2} for i, question in enumerate(questions)]
    return {"units": units, "concepts": concepts, "questions": questions, "attempts": attempts, "recovery_entries": []}


def test_adapter_keeps_same_display_codes_in_different_materials_independent_and_handles_null_concepts():
    result = calculate_learning_metrics(adapter_input())
    assert [(row.unit_id, row.material_id, row.score) for row in result.units] == [(uid(10), uid(11), 0), (uid(20), uid(21), 99)]
    assert [(row.concept_id, row.wrong_count) for row in result.concept_errors] == [(None, 1), (uid(12), 1)]


@pytest.mark.parametrize("field,value", [("finalized_at", None), ("graded_at", None), ("grading_status", "pending"),
    ("session_status", "ready"), ("session_finalized_at", None), ("session_grading_status", "pending"), ("is_correct", None)])
def test_adapter_rejects_unfinalized_or_ungraded_rows(field, value):
    data = adapter_input()
    data["attempts"][0][field] = value
    with pytest.raises(ValueError):
        calculate_learning_metrics(data)


def test_adapter_rejects_a_concept_from_another_unit_instead_of_merging_it_by_code():
    data = adapter_input()
    data["questions"][0]["concept_id"] = uid(22)
    with pytest.raises(ValueError):
        calculate_learning_metrics(data)


def test_scope_identity_changes_with_actual_range_source_and_concepts_but_not_mastery_updates():
    data = adapter_input()
    original = current_scope(data["units"][0], data["concepts"])
    unit = {**data["units"][0], "updated_at": AT + timedelta(days=1), "mastery_score": 99}
    assert current_scope(unit, data["concepts"]) == original
    for field, value in (("end_page", 9), ("in_scope", False), ("material_sha256", "b" * 64), ("analysis_version", "v2")):
        assert current_scope({**unit, field: value}, data["concepts"]) != original
    concepts = deepcopy(data["concepts"])
    concepts[0]["retired_at"] = AT
    assert current_scope(unit, concepts) != original


def test_adapter_does_not_fabricate_recovery_entries_or_bonus_from_legacy_unit_flags():
    data = adapter_input()
    data["attempts"] = []
    data["units"][0].update(mastery_score=70, recovery_mode=True, recovery_started_at=AT)
    result = calculate_learning_metrics(data)
    assert result.units == () and result.history == ()
    assert data["recovery_entries"] == []


def test_sqlite_naive_stored_timestamps_are_normalized_without_changing_stored_answer_date():
    data = adapter_input()
    expected = calculate_learning_metrics(data)
    for row in data["attempts"]:
        row["answered_at"] = row["answered_at"].replace(tzinfo=None)
        row["finalized_at"] = row["finalized_at"].replace(tzinfo=None)
    assert calculate_learning_metrics(data) == expected


@pytest.fixture
def mastery_case(domain_connection):
    conn = domain_connection
    user = replace(User.anonymous(now=AT), demo_date=DAY)
    UserRepository(conn).add(user)
    subject_id = str(uuid4())
    conn.execute(t.subjects.insert().values(id=subject_id, user_id=user.id, code="SUBJECT", name="과목", color="#123456",
                                           status="active", created_at=AT, updated_at=AT))
    units, questions = [], []
    for index in range(2):
        material_id, unit_id, concept_id = str(uuid4()), str(uuid4()), str(uuid4())
        conn.execute(t.materials.insert().values(id=material_id, subject_id=subject_id, original_name=f"{index}.pdf",
            source_format="pdf", sha256=str(index) * 64, analysis_version="v1", created_at=AT, updated_at=AT))
        conn.execute(t.units.insert().values(id=unit_id, material_id=material_id, code="UNIT1", title=f"자료{index} 단원1",
            sort_order=1, start_page=1, end_page=10, difficulty="low", created_at=AT, updated_at=AT))
        conn.execute(t.concepts.insert().values(id=concept_id, unit_id=unit_id, code="C1", title="개념", sort_order=1,
            start_page=1, end_page=5, created_at=AT, updated_at=AT))
        unit_questions = []
        for number in range(20):
            question_id = str(uuid4())
            conn.execute(t.questions.insert().values(id=question_id, unit_id=unit_id,
                concept_id=None if index == 0 and number == 0 else concept_id, code=f"Q{number + 1}", type="choice",
                difficulty="low", prompt=f"자료{index} 문항{number}", choices=[{"text": "맞음"}, {"text": "틀림"}],
                correct_answer="맞음", explanation="설명", created_at=AT))
            unit_questions.append(question_id)
        units.append({"id": unit_id, "material_id": material_id})
        questions.append(unit_questions)
    conn.commit()
    return {"user": user, "subject_id": subject_id, "units": units, "questions": questions,
            "headers": {"X-Anonymous-Id": user.id}}


def make_quiz(conn, case, questions, *, day=DAY, owner_id=None):
    quiz_id = str(uuid4())
    conn.execute(t.quiz_sessions.insert().values(id=quiz_id, user_id=owner_id or case["user"].id,
        subject_id=case["subject_id"], type="practice", quiz_date=day, status="ready", selected_difficulty="low", created_at=AT))
    items = [{"id": str(uuid4()), "quiz_session_id": quiz_id, "question_id": question_id,
              "sort_order": n, "inclusion_reason": "practice"} for n, question_id in enumerate(questions, 1)]
    conn.execute(t.quiz_items.insert(), items)
    conn.commit()
    return quiz_id, items


def submit_quiz(client, conn, case, questions, correct_count, *, day=DAY):
    quiz_id, items = make_quiz(conn, case, questions, day=day)
    response = client.post(f"/api/quiz/{quiz_id}/submit", headers=case["headers"], json={"quizId": quiz_id,
        "answers": [{"questionId": item["id"], "choice": 0 if n < correct_count else 1, "confidence": "sure"}
                    for n, item in enumerate(items)]})
    assert response.status_code == 200, response.text
    conn.commit()
    return quiz_id, response.json()["data"]


def test_http_same_codes_across_pdfs_and_mixed_null_concepts_do_not_merge_or_raise_500(api_client, domain_connection, mastery_case, monkeypatch):
    from app.services import quiz
    def forbidden(*args):
        raise AssertionError("Mastery and objective grading must not call private AI")
    monkeypatch.setattr(quiz.ai, "module", forbidden)
    case, conn = mastery_case, domain_connection
    first_id, first = submit_quiz(api_client, conn, case, case["questions"][0], 0)
    second_id, second = submit_quiz(api_client, conn, case, case["questions"][1], 20)
    assert [(row["unitId"], row["after"]) for row in first["unitChanges"]] == [(case["units"][0]["id"], 0)]
    assert [(row["unitId"], row["after"]) for row in second["unitChanges"]] == [(case["units"][1]["id"], 99)]
    stored = {row["id"]: row["mastery_score"] for row in conn.execute(select(t.units)).mappings()}
    assert stored == {case["units"][0]["id"]: 0, case["units"][1]["id"]: 99}
    raw = conn.scalar(select(t.mastery_history.c.raw_score).where(t.mastery_history.c.quiz_session_id == second_id))
    assert raw == 100
    assert api_client.get(f"/api/quiz/{first_id}/result", headers=case["headers"]).json()["data"] == first


def test_http_first_day_observed_entry_is_sticky_and_multiple_sets_do_not_stack_daily_cap(api_client, domain_connection, mastery_case):
    case, conn = mastery_case, domain_connection
    questions = case["questions"][0]
    first_id, first = submit_quiz(api_client, conn, case, questions[:10], 7)
    second_id, second = submit_quiz(api_client, conn, case, questions, 20)
    third_id, third = submit_quiz(api_client, conn, case, questions, 20)
    assert [result["unitChanges"][0]["after"] for result in (first, second, third)] == [70, 75, 75]
    rows = {row["quiz_session_id"]: row for row in conn.execute(select(t.mastery_history)).mappings()}
    assert rows[second_id]["raw_score"] == 97 and rows[third_id]["raw_score"] > 97
    assert rows[third_id]["daily_increase_limit"] == 5 and rows[third_id]["recorded_date"] == DAY
    assert rows[second_id]["previous_score"] == 70 and rows[third_id]["previous_score"] == 75
    svc = QuizService(conn, case["user"].to_mapping(), DAY)
    snapshot = dict(conn.execute(select(t.units).where(t.units.c.id == case["units"][0]["id"])).mappings().one())
    svc._recalc_mastery(third_id, {case["units"][0]["id"]})
    svc._recalc_mastery(third_id, {case["units"][0]["id"]})
    assert dict(conn.execute(select(t.units).where(t.units.c.id == snapshot["id"])).mappings().one()) == snapshot
    assert conn.scalar(select(func.count()).select_from(t.mastery_history)) == 3


def test_http_unverified_passes_do_not_unlock_stable_recovery_or_fifteen_bonus(api_client, domain_connection, mastery_case):
    case, conn = mastery_case, domain_connection
    _, first = submit_quiz(api_client, conn, case, case["questions"][0][:10], 7)
    conn.execute(t.learning_passes.insert().values(id=str(uuid4()), unit_id=case["units"][0]["id"], pass_number=2,
        completion_kind="remedial", evidence_key="unverified", evidence_snapshot={}, completed_at=AT, completed_date=DAY))
    conn.commit()
    scores = [first["unitChanges"][0]["after"]]
    for offset in (1, 2, 3):
        day = DAY + timedelta(days=offset)
        conn.execute(update(t.users).where(t.users.c.id == case["user"].id).values(demo_date=day))
        conn.commit()
        _, result = submit_quiz(api_client, conn, case, case["questions"][0], 20, day=day)
        scores.append(result["unitChanges"][0]["after"])
    assert scores == [70, 75, 79, 79]


@pytest.mark.parametrize("missing", ["attempt_finalized", "attempt_graded", "attempt_status", "session_finalized",
                                    "session_status", "session_grading", "foreign_owner"])
def test_http_does_not_apply_pending_or_foreign_correct_answers(api_client, domain_connection, mastery_case, missing):
    case, conn = mastery_case, domain_connection
    owner_id = None
    if missing == "foreign_owner":
        other = User.anonymous(now=AT)
        UserRepository(conn).add(other)
        conn.commit()
        owner_id = other.id
    stale_quiz, items = make_quiz(conn, case, case["questions"][0], owner_id=owner_id)
    session_values = {"status": "submitted", "finalized_at": AT, "grading_status": "completed", "submitted_at": AT}
    session_values.update({"session_finalized": {"finalized_at": None}, "session_status": {"status": "in_progress"},
                           "session_grading": {"grading_status": "pending"}}.get(missing, {}))
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == stale_quiz).values(**session_values))
    for item in items:
        values = {"id": str(uuid4()), "quiz_item_id": item["id"], "answer": "맞음", "confidence": "sure", "is_correct": True,
                  "grading_status": "graded", "answered_at": AT, "answered_date": DAY, "finalized_at": AT, "graded_at": AT}
        values.update({"attempt_finalized": {"finalized_at": None}, "attempt_graded": {"graded_at": None},
                       "attempt_status": {"grading_status": "pending"}}.get(missing, {}))
        conn.execute(t.quiz_attempts.insert().values(**values))
    conn.commit()
    _, result = submit_quiz(api_client, conn, case, case["questions"][0], 0)
    assert result["unitChanges"][0]["after"] == 0
    assert conn.scalar(select(func.count()).select_from(t.mastery_history)) == 1


def test_seed_flags_without_actual_history_never_create_a_recovery_anchor(domain_connection, mastery_case):
    case, conn = mastery_case, domain_connection
    conn.execute(update(t.units).where(t.units.c.id == case["units"][0]["id"])
                 .values(mastery_score=70, recovery_mode=True, recovery_started_at=AT))
    svc = QuizService(conn, case["user"].to_mapping(), DAY)
    data = svc._metrics_input()
    assert data["recovery_entries"] == [] and data["attempts"] == []
    assert calculate_learning_metrics(data).units == ()


@pytest.mark.parametrize("foreign", [False, True], ids=["ungraded-history", "foreign-history"])
def test_ungraded_or_foreign_history_cannot_create_a_recovery_anchor(api_client, domain_connection, mastery_case, foreign):
    case, conn = mastery_case, domain_connection
    other = User.anonymous(now=AT)
    UserRepository(conn).add(other)
    conn.commit()
    quiz_id, _ = make_quiz(conn, case, case["questions"][0], owner_id=other.id if foreign else None)
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == quiz_id).values(
        status="submitted", finalized_at=AT, grading_status="completed" if foreign else "pending"))
    conn.execute(t.mastery_history.insert().values(id=str(uuid4()), unit_id=case["units"][0]["id"], quiz_session_id=quiz_id,
        raw_score=70, score=70, level="shaky", recovery_mode=True, recorded_date=DAY, created_at=AT))
    conn.commit()
    _, result = submit_quiz(api_client, conn, case, case["questions"][0], 20)
    assert result["unitChanges"][0]["after"] == 99 and result["unitChanges"][0]["grade"] == "stable"
