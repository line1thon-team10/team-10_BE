"""Durable pace overflow input for the P2 allocation consumer."""
from collections import defaultdict
from uuid import uuid4

from sqlalchemy import select
from app.db import schema as t
from app.domain.ranges import PageSpan, page_count
from app.services.study import StudyData


def request_pace_recovery(conn, user, today, task_ids, over_days, *, at, calibration_id):
    data = StudyData(conn, user['id'])
    selected = [task for task in data.tasks if task.id in task_ids]
    by_subject = defaultdict(list)
    for task in selected:
        by_subject[task.row['subject_id']].extend(PageSpan(row['material_id'], row['start_page'], row['end_page']) for row in task.paged)
    context = {'reason':'pace', 'calibrationId':calibration_id, 'assessedDate':today.isoformat(),
        'tasks':[{'taskId':task.id, 'ranges':[{'unit_id':row['unit_id'],'start_page':row['start_page'],
            'end_page':row['end_page']} for row in task.paged]} for task in selected],
        'subjects':[{'subjectId':sid,'overduePages':page_count(spans),'dailyAveragePages':0,'thresholdPages':0}
            for sid,spans in sorted(by_subject.items())], 'overDays':over_days}
    identifier = str(uuid4())
    conn.execute(t.plan_change_requests.insert().values(id=identifier,user_id=user['id'],base_plan_id=data.plan['id'],
        # The caller's ProposalService advances this plan after its callback.
        base_plan_revision=data.plan['revision']+1,kind='recovery',status='ready',recovery_context=context,
        input_payload={'schemaVersion':'pace-recovery-v1','calibrationId':calibration_id},created_at=at,updated_at=at))
    return identifier
