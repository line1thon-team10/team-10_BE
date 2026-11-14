"""Carryover and its threshold count physical PDF pages once, without filling gaps."""
from app.services.planner import remaining_ranges, _pages
from app.services.study import Task


def test_remaining_union_keeps_holes_source_identity_and_distinct_pdfs():
    task = Task({'id': 'task'}, ranges=[
        {'id': 'r1', 'unit_id': 'u1', 'material_id': 'pdf1', 'start_page': 1, 'end_page': 8},
        {'id': 'r2', 'unit_id': 'u2', 'material_id': 'pdf1', 'start_page': 6, 'end_page': 12},
        {'id': 'r3', 'unit_id': 'u3', 'material_id': 'pdf2', 'start_page': 1, 'end_page': 3},
    ], done_ranges=[
        {'unit_id': 'u1', 'start_page': 3, 'end_page': 4},
        {'unit_id': 'u2', 'start_page': 7, 'end_page': 10},
        {'unit_id': 'u1', 'start_page': 3, 'end_page': 3},
    ])
    rest = remaining_ranges(task)
    assert [(r['source_range_id'], r['material_id'], r['start_page'], r['end_page']) for r in rest] == [
        ('r1', 'pdf1', 1, 2), ('r1', 'pdf1', 5, 6), ('r2', 'pdf1', 11, 12), ('r3', 'pdf2', 1, 3)]
    assert _pages(rest) == 9
    assert _pages([*rest, *rest]) == 9


def test_nonpage_and_fully_completed_ranges_do_not_carry():
    task = Task({'id': 'task'}, ranges=[
        {'id': 'r1', 'unit_id': 'u1', 'material_id': 'pdf1', 'start_page': 1, 'end_page': 10},
        {'id': 'r2', 'unit_id': 'u2', 'material_id': 'pdf2', 'start_page': None, 'end_page': None},
    ], done_ranges=[{'unit_id': 'u1', 'start_page': 1, 'end_page': 10}])
    assert remaining_ranges(task) == []
