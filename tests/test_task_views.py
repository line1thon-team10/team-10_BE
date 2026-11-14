"""Shared read projection and its real home/focus HTTP consumers."""
from copy import deepcopy

import pytest

from app.services.task_views import TaskView


def planned(identifier="range-a", material="pdf-a", unit="unit-a", start=1, end=5, concept=None):
    return {"id": identifier, "material_id": material, "unit_id": unit, "concept_id": concept,
            "start_page": start, "end_page": end}


def completed(unit="unit-a", start=1, end=5):
    return {"unit_id": unit, "start_page": start, "end_page": end}


def test_task_view_preserves_order_identity_and_unions_pages_per_pdf():
    ranges = [planned("r-b", "pdf-b", "unit-b", concept="concept-b"),
              planned("r-a", end=7), planned("r-overlap", start=3, end=5)]
    done = [completed("unit-b", 1, 2), completed(start=1, end=4), completed(start=3, end=4)]
    before = deepcopy((ranges, done))

    view = TaskView.from_ranges(iter(ranges), iter(done), unit_code="AI2").as_dict()

    assert (view["pages"], view["donePages"]) == (12, 6)
    assert view["from"] is view["to"] is view["doneTo"] is view["unitNo"] is None
    assert view["ranges"] == [
        {"id": "r-b", "materialId": "pdf-b", "unitId": "unit-b", "conceptId": "concept-b",
         "from": 1, "to": 5, "doneTo": 2},
        {"id": "r-a", "materialId": "pdf-a", "unitId": "unit-a", "conceptId": None,
         "from": 1, "to": 7, "doneTo": 4},
        {"id": "r-overlap", "materialId": "pdf-a", "unitId": "unit-a", "conceptId": None,
         "from": 3, "to": 5, "doneTo": 4},
    ]
    assert (ranges, done) == before


@pytest.mark.parametrize("done,done_pages,done_to", [
    ([], 0, None),
    ([completed(start=1, end=20)], 8, 10),
    ([completed(start=1, end=4), completed(start=8, end=20)], 5, 4),
    ([completed(start=8, end=20)], 3, None),
    ([completed(start=3, end=4), completed(start=5, end=7)], 5, 7),
    ([completed(start=1, end=2), completed(start=11, end=20)], 0, None),
    ([completed("other-unit", 3, 10)], 0, None),
    ([completed(start=None, end=None)], 0, None),
])
def test_task_view_clips_current_unit_evidence_and_keeps_contiguous_prefix(done, done_pages, done_to):
    view = TaskView.from_ranges([planned(start=3, end=10)], done, unit_code="AI2").as_dict()
    assert (view["from"], view["to"], view["pages"], view["unitNo"]) == (3, 10, 8, 2)
    assert view["donePages"] == done_pages
    assert view["doneTo"] == view["ranges"][0]["doneTo"] == done_to


def test_task_view_overlapping_units_share_page_totals_but_not_completion_evidence():
    view = TaskView.from_ranges([planned(), planned("r-b", unit="unit-b")], [completed()]).as_dict()
    assert (view["pages"], view["donePages"]) == (5, 5)
    assert [item["doneTo"] for item in view["ranges"]] == [5, None]


@pytest.mark.parametrize("ranges", [[], [planned(start=None, end=None)]])
def test_task_view_empty_or_nominal_ranges_do_not_imply_completion(ranges):
    view = TaskView.from_ranges(ranges, [completed()], unit_code="AI2").as_dict()
    assert view == {"ranges": [], "pages": 0, "donePages": 0,
                    "from": None, "to": None, "doneTo": None, "unitNo": None}


def test_task_view_paged_and_nominal_mix_has_no_single_range_shortcut():
    view = TaskView.from_ranges([planned(), planned("nominal", start=None, end=None)],
                                [completed()], unit_code="AI2").as_dict()
    assert (view["pages"], view["donePages"], len(view["ranges"])) == (5, 5, 1)
    assert view["from"] is view["to"] is view["doneTo"] is view["unitNo"] is None


def test_task_view_counts_large_ranges_without_materializing_each_page():
    view = TaskView.from_ranges([planned(end=10**12)], [completed(end=10**12 - 1)])
    assert view.pages == 10**12
    assert view.done_pages == view.done_to == 10**12 - 1


@pytest.mark.parametrize("start,end", [(0, 5), (5, 4), (True, 5), (1.0, 5), (1, "5")])
def test_task_view_does_not_coerce_invalid_page_bounds(start, end):
    with pytest.raises(ValueError):
        TaskView.from_ranges([planned(start=start, end=end)])
