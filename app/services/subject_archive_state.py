"""Stable archive cutoffs, including the demonstration calendar."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.core.clock import today_for
from app.db import schema as t


def archived_dates(conn, user, subjects, *, include_legacy=True):
    archived = {subject['id']:subject for subject in subjects if subject['status']=='archived'}
    if not archived:
        return {}
    result = {}
    states = conn.execute(select(t.plan_proposal_states.c.target, t.plan_proposal_states.c.base_date)
        .where(t.plan_proposal_states.c.user_id==user['id'], t.plan_proposal_states.c.kind=='subject_archive',
            t.plan_proposal_states.c.status=='applied').order_by(t.plan_proposal_states.c.decided_at,
                t.plan_proposal_states.c.proposal_id)).mappings()
    for row in states:
        subject_id = (row['target'] or {}).get('subjectId')
        if subject_id in archived and subject_id not in result:
            result[subject_id] = row['base_date']
    for subject_id,subject in archived.items():
        if subject_id in result:
            continue
        if subject.get('archive_reason')=='exam_finished' and subject.get('exam_date'):
            result[subject_id] = subject['exam_date']+timedelta(days=1)
            continue
        value = subject.get('archived_at')
        if value is not None:
            result[subject_id] = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo(user['timezone'])).date()
        elif include_legacy:
            result[subject_id] = today_for(user)
    return result


def archived_date(conn, user, subject):
    return archived_dates(conn,user,[subject]).get(subject['id'])
