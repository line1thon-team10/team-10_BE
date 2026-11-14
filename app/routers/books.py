"""책 쌓기 S9 · 시험 준비도 (BK-01~05, KP-01) — 프론트 EP.books"""

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, func, select

from app.core.auth import current_user
from app.core.clock import today_for
from app.core.responses import ok
from app.db import schema as t
from app.db.session import get_conn
from app.services.study import StudyData
from app.services.book_views import BookViews

router = APIRouter(prefix="/api", tags=["책 쌓기"])


@router.get("/books")
def get_books(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    today = today_for(user)
    data = StudyData(conn, user["id"])
    books = BookViews(data, today)
    rounds = dict(conn.execute(
        select(t.learning_passes.c.unit_id, func.count(t.learning_passes.c.id))
        .where(t.learning_passes.c.unit_id.in_(list(books.units)), t.learning_passes.c.invalidated_at.is_(None))
        .group_by(t.learning_passes.c.unit_id)).all())

    blocks_by_unit = books.by_unit()

    subjects = []
    for s in data.subjects:
        units = []
        for u in sorted((u for u in books.units.values() if u["subject_id"] == s["id"]),
                        key=lambda u: (u["material_id"], u["sort_order"], u["id"])):
            units.append({
                "id": u["id"], "materialId": u["material_id"], "no": u["sort_order"], "title": u["title"], "from": u["start_page"], "to": u["end_page"],
                "grade": u["mastery_level"], "score": u["mastery_score"], "round": rounds.get(u["id"], 0),
                "note": None, "blocks": blocks_by_unit[u["id"]],
            })
        all_blocks = [b for u in units for b in u["blocks"]]
        subjects.append({"subjectId": s["id"], "name": s["name"], "color": s["color"],
                         "total": len(all_blocks),
                         "collected": sum(1 for b in all_blocks if b["state"] != "planned"),
                         "units": units})
    return ok({"subjects": subjects, "readiness": [data.readiness(s) for s in data.subjects]})
