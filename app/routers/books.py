"""책 쌓기 S9 · 시험 준비도 (BK-01~05, KP-01) — 프론트 EP.books"""
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, func, select

from app.core.auth import current_user
from app.core.clock import today_for
from app.core.responses import ok
from app.db import schema as t
from app.db.session import get_conn
from app.services.study import StudyData, unit_no

router = APIRouter(prefix="/api", tags=["책 쌓기"])


@router.get("/books")
def get_books(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    today = today_for(user)
    data = StudyData(conn, user["id"])
    rounds = dict(conn.execute(
        select(t.learning_passes.c.unit_id, func.max(t.learning_passes.c.pass_number))
        .where(t.learning_passes.c.unit_id.in_(list(data.units)), t.learning_passes.c.invalidated_at.is_(None))
        .group_by(t.learning_passes.c.unit_id)).all())

    blocks_by_unit = defaultdict(list)
    for x in data.tasks:  # 블록 1개 = 태스크 1개. 단원이 없는 총복습은 단원 책장에 넣지 않아요
        unit = data.unit_of(x)
        if unit is None or x.row["status"] in ("carried_over", "excluded"):
            continue
        blocks_by_unit[unit["id"]].append({
            "state": data.block_state(x, today),
            "supplement": x.row["task_type"] == "remedial",
            "task": {"id": x.id, "title": x.row["title"], "from": x.first, "to": x.last, "doneTo": x.done_to},
        })

    subjects = []
    for s in data.subjects:
        units = []
        for u in [u for u in data.units.values() if u["subject_id"] == s["id"]]:
            units.append({
                "no": unit_no(u["code"]), "title": u["title"], "from": u["start_page"], "to": u["end_page"],
                "grade": u["mastery_level"], "score": u["mastery_score"], "round": rounds.get(u["id"], 1),
                "note": None, "blocks": blocks_by_unit[u["id"]],
            })
        all_blocks = [b for u in units for b in u["blocks"]]
        subjects.append({"subjectId": s["id"], "name": s["name"], "color": s["color"],
                         "total": len(all_blocks),
                         "collected": sum(1 for b in all_blocks if b["state"] != "planned"),
                         "units": units})
    return ok({"subjects": subjects, "readiness": [data.readiness(s) for s in data.subjects]})
