"""Once-per-day P1 admission, using raw placement progress under the user lock."""
from datetime import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import schema as t
from app.domain.progress import PageProgress


def day_summary(conn, user, data, day, at, *, before_quiz=None):
    """Caller holds the user mutation lock and commits notice with its response."""
    tasks = data.tasks_on(day)
    progress = data.progress(day)
    recorded = sum(task.record is not None for task in tasks)
    all_recorded = bool(tasks) and recorded == len(tasks)
    late = at.astimezone(ZoneInfo(user.get('timezone') or 'Asia/Seoul')).time() >= time(23,30)
    if not (all_recorded or late) or not PageProgress(progress['donePages'],progress['totalPages']).reaches(70):
        return None
    dedupe = f'day_summary:{day.isoformat()}'
    if conn.scalar(select(t.user_notices.c.id).where(t.user_notices.c.user_id == user['id'],
            t.user_notices.c.dedupe_key == dedupe)) is not None:
        return None
    quiz = data.quiz_progress(day)
    counts = data.quiz_counts(day)
    opened = [] if before_quiz is None else [
        {'subjectId': sid, 'count': counts.get(sid,0)} for sid, yes in quiz['subjectOpen'].items()
        if yes and not before_quiz['subjectOpen'].get(sid,False)]
    event = {'type':'box','reason':'dayClose','rate':progress['rate'],
             'recordedCount':recorded,'totalCount':progress['totalCount'],
             'donePages':progress['donePages'],'totalPages':progress['totalPages'],'opened':opened}
    conn.execute(t.user_notices.insert().values(id=str(uuid4()),user_id=user['id'],dedupe_key=dedupe,
        notice_type='day_summary',notice_date=day,status='shown',payload=event,created_at=at,shown_at=at))
    return event
