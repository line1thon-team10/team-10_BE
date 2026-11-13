"""기록 정정 후 기존 회독의 고정 근거만 재검사한다. 새 회차는 만들지 않는다."""
from uuid import UUID

from sqlalchemy import Connection, select, update

from app.db import schema as t


def _required_pages(snapshot: dict) -> list[tuple[int, int]]:
    ranges = snapshot.get("requiredRanges")
    if snapshot.get("schemaVersion") != "1" or not isinstance(ranges, list) or not ranges:
        raise ValueError("Unsupported learning evidence snapshot; record change rolled back")
    if any(not isinstance(pair, list) or len(pair) != 2
           or any(type(page) is not int for page in pair)
           or not 1 <= pair[0] <= pair[1] for pair in ranges):
        # 명목 분량/새 버전은 추측해서 기존 자격을 지우거나 유지하지 않는다.
        raise ValueError("Unsupported learning evidence ranges; record change rolled back")
    return [tuple(pair) for pair in ranges]


def _covers(required: list[tuple[int, int]], actual: list[tuple[int, int]]) -> bool:
    merged: list[list[int]] = []
    for start, end in sorted(actual):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return all(any(left <= start and end <= right for left, right in merged) for start, end in required)


def revalidate_record_passes(conn: Connection, user_id: str, record_id: str, at) -> None:
    """호출자가 사용자 잠금을 보유한다. 기록/회독 변경은 같은 트랜잭션이다."""
    passes = conn.execute(select(t.learning_passes)
                          .join(t.units, t.units.c.id == t.learning_passes.c.unit_id)
                          .join(t.materials, t.materials.c.id == t.units.c.material_id)
                          .join(t.subjects, t.subjects.c.id == t.materials.c.subject_id)
                          .where(t.subjects.c.user_id == user_id)).mappings().all()
    for item in passes:
        snapshot = item["evidence_snapshot"]
        ids = snapshot.get("recordIds") if isinstance(snapshot, dict) else None
        if not isinstance(ids, list) or record_id not in ids:
            continue
        required = _required_pages(snapshot)
        # UUID 검증 후 조회해 PostgreSQL/SQLite가 잘못된 저장 근거를 다르게 해석하지 않게 한다.
        ids = {str(UUID(value)) for value in ids}
        owned = set(conn.scalars(select(t.study_records.c.id)
                                 .join(t.study_tasks, t.study_tasks.c.id == t.study_records.c.task_id)
                                 .join(t.subjects, t.subjects.c.id == t.study_tasks.c.subject_id)
                                 .where(t.study_records.c.id.in_(ids), t.subjects.c.user_id == user_id)))
        actual = conn.execute(select(t.study_record_ranges.c.start_page, t.study_record_ranges.c.end_page)
                              .join(t.study_records, t.study_records.c.id == t.study_record_ranges.c.record_id)
                              .where(t.study_record_ranges.c.record_id.in_(owned),
                                     t.study_record_ranges.c.unit_id == item["unit_id"],
                                     t.study_records.c.result.in_(["completed", "partial"]),
                                     t.study_record_ranges.c.start_page.is_not(None),
                                     t.study_record_ranges.c.end_page.is_not(None))).all()
        valid = owned == ids and _covers(required, actual)
        if valid and item["invalidated_at"] is not None:
            conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == item["id"])
                         .values(invalidated_at=None))
        elif not valid and item["invalidated_at"] is None:
            conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == item["id"])
                         .values(invalidated_at=at))
