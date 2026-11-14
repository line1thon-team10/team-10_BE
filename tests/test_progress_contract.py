"""Separate placement achievement, unique study progress and quiz eligibility."""
from fractions import Fraction
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.domain.progress import PageProgress, rounded_percent
from app.domain.ranges import PageSpan
from app.services.study import StudyData


class ProgressWorld:
    """Keep the real demo owner/plan, with explicitly controlled new evidence."""

    def __init__(self, conn, user):
        self.conn, self.user = conn, user
        self.day = date(2026, 11, 9)
        self.now = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)
        data = StudyData(conn, user)
        self.subject = next(s for s in data.subjects if s["code"] == "AI")
        self.plan = data.plan
        self.material_template = dict(conn.execute(select(t.materials).where(
            t.materials.c.subject_id == self.subject["id"])).mappings().first())
        self.unit_template = next(u for u in data.units.values() if u["subject_id"] == self.subject["id"])
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.plan_id == self.plan["id"]).values(status="excluded"))
        conn.execute(update(t.units).where(t.units.c.id.in_(list(data.units))).values(in_scope=False))

    def unit(self, start=1, end=100, *, score=80, quiz_enabled=True, material_id=None, **values):
        if material_id is None:
            material_id = str(uuid4())
            self.conn.execute(t.materials.insert().values(**{
                **self.material_template, "id": material_id, "sha256": uuid4().hex,
                "page_count": end, "quiz_enabled": quiz_enabled,
            }))
        unit = {key: value for key, value in self.unit_template.items() if key in t.units.c}
        unit.update({"id": str(uuid4()), "material_id": material_id, "code": "TEST1", "start_page": start,
                     "end_page": end, "mastery_score": score, "in_scope": True, "retired_at": None, **values})
        self.conn.execute(t.units.insert().values(**unit))
        return unit

    def concept(self, unit, start, end, **values):
        concept = {"id": str(uuid4()), "unit_id": unit["id"], "code": "C1", "title": "시험 범위", "sort_order": 0,
                   "start_page": start, "end_page": end, "in_scope": True, "created_at": self.now,
                   "updated_at": self.now, **values}
        self.conn.execute(t.concepts.insert().values(**concept))
        return concept

    def task(self, *parts, done=None, kind="study", status="planned", day=None):
        day = day or self.day
        task_id, block_id = str(uuid4()), str(uuid4())
        self.conn.execute(t.study_blocks.insert().values(
            id=block_id, subject_id=self.subject["id"], unit_id=parts[0][0]["id"] if parts else None,
            task_type=kind, created_at=self.now))
        self.conn.execute(t.study_tasks.insert().values(
            id=task_id, plan_id=self.plan["id"], subject_id=self.subject["id"], block_id=block_id,
            task_type=kind, title="진행률 경계", scheduled_date=day, estimated_minutes=25,
            status=status, created_at=self.now))
        for order, (unit, start, end) in enumerate(parts):
            self.conn.execute(t.study_task_ranges.insert().values(
                id=str(uuid4()), task_id=task_id, unit_id=unit["id"], start_page=start, end_page=end,
                sort_order=order))
        if done is not None:
            record_id = str(uuid4())
            self.conn.execute(t.study_records.insert().values(
                id=record_id, task_id=task_id, result="completed" if status == "completed" else "partial" if done else "none",
                recorded_at=self.now, study_date=day, updated_at=self.now))
            for unit, start, end in done:
                self.conn.execute(t.study_record_ranges.insert().values(
                    id=str(uuid4()), record_id=record_id, unit_id=unit["id"], start_page=start, end_page=end))
        return task_id

    def data(self):
        return StudyData(self.conn, self.user)

    def get(self, client, path):
        self.conn.commit()
        response = client.get(path, headers={"X-Anonymous-Id": self.user})
        assert response.status_code == 200, response.text
        return response.json()["data"]


@pytest.fixture
def progress_world(domain_connection, demo_user):
    return ProgressWorld(domain_connection, demo_user)


@pytest.mark.parametrize("done,total,rate,opened,remaining", [
    (0, 0, 0, False, 0), (10, 0, 0, False, 0),
    (1, 8, 13, False, 5), (6999, 10000, 70, False, 1),
    (7, 10, 70, True, 0), (101, 100, 100, True, 0),
    (7 * 10 ** 30 - 1, 10 ** 31, 70, False, 1),
])
def test_page_ratio_keeps_display_and_raw_threshold_separate(done, total, rate, opened, remaining):
    progress = PageProgress(done, total)
    assert progress.rate == rate
    assert progress.reaches(70) is opened
    assert progress.remaining_pages(70) == remaining


@pytest.mark.parametrize("invalid", [-1, True, 1.0, "1", None, float("nan"), float("inf")])
def test_page_counts_are_nonnegative_integers(invalid):
    with pytest.raises(ValueError):
        PageProgress(invalid, 10)
    with pytest.raises(ValueError):
        PageProgress(1, invalid)


@pytest.mark.parametrize("invalid", [-1, 101, True, 70.0, "70", None])
def test_thresholds_reject_ambiguous_inputs(invalid):
    progress = PageProgress(7, 10)
    with pytest.raises(ValueError):
        progress.reaches(invalid)
    with pytest.raises(ValueError):
        progress.remaining_pages(invalid)


def test_unique_progress_intersects_scope_and_preserves_pdf_identity():
    progress = PageProgress.from_ranges(
        iter([PageSpan("a", 1, 5), PageSpan("a", 4, 10), PageSpan("b", 1, 10)]),
        [PageSpan("a", 4, 30), PageSpan("a", 5, 8), PageSpan("b", 1, 5), PageSpan("c", 1, 10)],
    )
    assert (progress.done_pages, progress.total_pages, progress.rate) == (12, 20, 60)


def test_percentage_display_uses_half_up_and_bounds():
    assert rounded_percent(Fraction(1, 16), places=1) == 6.3
    assert rounded_percent(Fraction(1, 8)) == 13
    assert rounded_percent(Fraction(2), places=1) == 100.0
    assert rounded_percent(Fraction(-1)) == 0


def test_home_achievement_counts_repeated_placements_and_each_pdf(api_client, progress_world):
    world = progress_world
    first, second = world.unit(1, 8), world.unit(1, 8)
    study = world.task((first, 1, 4), (first, 3, 8), (second, 1, 8),
                       done=[(first, 1, 8)], status="partial")
    remedial = world.task((first, 1, 8), done=[(first, 1, 8)], kind="remedial", status="completed")
    world.task((first, 1, 8), done=[(first, 1, 8)], status="carried_over")
    world.task((first, 1, 8), status="excluded")
    home = world.get(api_client, "/api/today")

    assert {task["id"] for task in home["tasks"]} == {study, remedial}
    assert home["progress"] == {"doneCount": 1, "totalCount": 2, "donePages": 16, "totalPages": 24, "rate": 67}
    assert home["subjectRates"][world.subject["id"]] == 67
    assert next(task for task in home["tasks"] if task["id"] == study)["pages"] == 16


def test_home_half_up_and_unpaged_zero_denominator(api_client, progress_world):
    world = progress_world
    unit = world.unit(1, 8)
    task = world.task((unit, 1, 8), done=[(unit, 1, 1)], status="partial")
    home = world.get(api_client, "/api/today")
    assert home["progress"]["rate"] == home["subjectRates"][world.subject["id"]] == 13

    world.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task).values(status="excluded"))
    world.task(done=[], status="completed", kind="review")
    home = world.get(api_client, "/api/today")
    assert home["progress"] == {"doneCount": 1, "totalCount": 1, "donePages": 0, "totalPages": 0, "rate": 0}
    assert home["tasks"][0]["ranges"] == []


def test_subject_progress_and_mastery_use_effective_concept_union_per_pdf(api_client, progress_world):
    world = progress_world
    first, second = world.unit(1, 10, score=80), world.unit(1, 10, score=20)
    world.concept(first, 1, 6)
    world.concept(first, 4, 8)
    world.concept(first, 9, 10, in_scope=False)
    world.task((first, 1, 10), done=[(first, 1, 10)], status="completed")
    world.task((second, 1, 10), done=[(second, 1, 5)], status="partial")
    # The same code/page numbers in a different PDF remain a separate scope.
    assert first["code"] == second["code"]
    books = world.get(api_client, "/api/books")
    result = next(row for row in books["readiness"] if row["subjectId"] == world.subject["id"])
    assert result == {"subjectId": world.subject["id"], "readiness": 34, "target": world.subject["target_score"],
                      "targetLabel": world.subject["target_grade"], "progressRate": 72.2, "mastery": 46.7,
                      "studiedPages": 13, "totalPages": 18}
    home = world.get(api_client, "/api/today")
    assert next(s for s in home["exams"] if s["subjectId"] == world.subject["id"])["readiness"] == 34


def test_quizless_pages_count_for_progress_but_never_mastery(progress_world):
    world = progress_world
    quiz_unit = world.unit(1, 10, score=80)
    manual = world.unit(1, 10, score=20, quiz_enabled=False)
    world.task((manual, 1, 10), done=[(manual, 1, 10)], status="completed")
    result = world.data().readiness(world.subject)
    assert (result["studiedPages"], result["totalPages"], result["progressRate"]) == (10, 20, 50)
    assert (result["mastery"], result["readiness"]) == (80, 40)

    world.conn.execute(update(t.units).where(t.units.c.id == quiz_unit["id"]).values(mastery_score=None))
    result = world.data().readiness(world.subject)
    assert result["mastery"] is None and result["readiness"] == 0


def test_subject_progress_keeps_carried_excluded_learning_but_not_remedial_or_retired_identity(progress_world):
    world = progress_world
    current = world.unit(1, 10)
    retired = world.unit(1, 10, material_id=current["material_id"], retired_at=world.now)
    world.task((current, 1, 5), done=[(current, 1, 50)], status="completed")
    world.task((current, 1, 10), done=[(current, 1, 10)], status="completed", kind="remedial")
    world.task((current, 6, 7), done=[(current, 6, 7)], status="carried_over", day=world.day - timedelta(days=1))
    world.task((current, 8, 9), done=[(current, 8, 9)], status="excluded", day=world.day - timedelta(days=2))
    world.task((retired, 10, 10), done=[(retired, 10, 10)], status="completed")
    # A different unit's record must not complete the current unit's same page.
    world.task((current, 10, 10), done=[(retired, 10, 10)], status="partial")
    result = world.data().readiness(world.subject)
    assert (result["studiedPages"], result["totalPages"], result["progressRate"]) == (9, 10, 90)
    assert result["mastery"] == 80 and result["readiness"] == 72


@pytest.mark.parametrize("other_state", ["active", "excluded", "retired", "concepts_excluded", "concepts_disjoint"])
def test_overlapping_current_units_have_no_invented_mastery_weight(progress_world, other_state):
    world = progress_world
    first = world.unit(1, 10, score=80)
    other = world.unit(5, 15, score=20, material_id=first["material_id"],
                       in_scope=other_state != "excluded", retired_at=world.now if other_state == "retired" else None)
    if other_state == "concepts_excluded":
        world.concept(other, 5, 15, in_scope=False)
    elif other_state == "concepts_disjoint":
        world.concept(other, 11, 15)
    world.task((first, 1, 10), done=[(first, 1, 10)], status="completed")
    result = world.data().readiness(world.subject)
    assert result["studiedPages"] == 10
    if other_state == "active":
        assert result["totalPages"] == 15 and result["progressRate"] == 66.7
        assert result["mastery"] is None and result["readiness"] == 0
    elif other_state == "concepts_disjoint":
        assert result["totalPages"] == 15 and result["progressRate"] == 66.7
        assert result["mastery"] == 60 and result["readiness"] == 40
    else:
        assert result["totalPages"] == 10 and result["progressRate"] == 100
        assert result["mastery"] == 80 and result["readiness"] == 80


def test_excluded_concepts_unpaged_and_archived_scopes_have_no_page_weight(progress_world):
    world = progress_world
    excluded = world.unit(1, 10)
    world.concept(excluded, 1, 10, in_scope=False)
    world.unit(None, None, score=99, quantity_basis="time")
    world.task((excluded, 1, 10), done=[(excluded, 1, 10)], status="completed")
    result = world.data().readiness(world.subject)
    assert (result["studiedPages"], result["totalPages"], result["progressRate"]) == (0, 0, 0)
    assert result["mastery"] is None and result["readiness"] == 0

    world.conn.execute(update(t.subjects).where(t.subjects.c.id == world.subject["id"]).values(status="archived"))
    result = world.data().readiness(world.subject)
    assert (result["studiedPages"], result["totalPages"], result["readiness"]) == (0, 0, 0)
    assert result["mastery"] is None


@pytest.mark.parametrize("done,total,score,shown_progress,readiness", [
    (1, 8, 20, 12.5, 3),  # 2.5 rounds up, not banker's 2.
    (3496, 10000, 10, 35.0, 3),  # Multiplying displayed 35.0 * 10 would incorrectly round to 4.
])
def test_readiness_uses_unrounded_inputs_and_half_up(progress_world, done, total, score, shown_progress, readiness):
    world = progress_world
    unit = world.unit(1, total, score=score)
    world.task((unit, 1, total), done=[(unit, 1, done)], status="partial")
    result = world.data().readiness(world.subject)
    assert result["progressRate"] == shown_progress and result["mastery"] == score
    assert result["readiness"] == readiness


def test_post_and_patch_replace_progress_evidence_without_duplicate_credit(api_client, progress_world):
    world = progress_world
    unit = world.unit(1, 10)
    task = world.task((unit, 1, 10))
    world.conn.commit()
    headers = {"X-Anonymous-Id": world.user}
    response = api_client.post("/api/records", headers=headers, json={"taskId": task, "result": "done"})
    assert response.status_code == 200, response.text
    assert world.data().readiness(world.subject)["studiedPages"] == 10
    world.conn.commit()

    response = api_client.patch(f"/api/tasks/{task}/record", headers=headers, json={"toPage": 3})
    assert response.status_code == 200, response.text
    data = world.data()
    assert (data.readiness(world.subject)["studiedPages"], data.readiness(world.subject)["progressRate"]) == (3, 30)
    assert data.progress(world.day)["donePages"] == 3
    assert len(list(world.conn.scalars(select(t.study_records.c.id).where(t.study_records.c.task_id == task)))) == 1
