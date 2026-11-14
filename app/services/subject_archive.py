"""Archive subject membership while preserving study and quiz facts."""
from datetime import timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, update
from app.core.responses import ApiError
from app.db import schema as t
from app.services.plan_draft import _basis
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService


def _apply(conn, context, changes):
    conn.execute(update(t.subjects).where(t.subjects.c.id==changes['subjectId'], t.subjects.c.user_id==context.user.id)
        .values(status='archived', archive_reason='user_removed', archived_at=context.now, updated_at=context.now))
    if changes['excludeIds']:
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id.in_(changes['excludeIds']),
            t.study_tasks.c.subject_id==changes['subjectId']).values(status='excluded'))
    return {'slots':changes['slots']}


def _after_apply(conn, context, response):
    if not response['slots']:
        return {'emptyTime':None, 'slots':[], 'options':[], 'previewId':None}
    from app.services.empty_time import EmptyTimeService
    return EmptyTimeService(conn, context.user.to_mapping()).offer(response['slots'])


DOMAINS = {existing:PreviewDomain('subject_archive', PlanEffect.UPDATE if existing else PlanEffect.DRAFT,
    _basis, _apply, after_apply=_after_apply) for existing in (False,True)}


def _local(value, tz):
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(tz)


class SubjectArchiveService:
    def __init__(self, engine):
        self.engine = engine

    def preview(self, user_id, subject_id):
        with self.engine.begin() as conn:
            subject = conn.execute(select(t.subjects).where(t.subjects.c.id==subject_id,
                t.subjects.c.user_id==user_id)).mappings().first()
            if subject is None:
                raise ApiError('NO_SUBJECT', '과목을 찾지 못했어요', 404)
            if subject['status'] == 'archived':
                raise ApiError('NO_CHANGE', '이미 보관된 과목이에요', 400)
            existing = conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id==user_id)) is not None
            domain = DOMAINS[existing]
            service = ProposalService(conn)
            snapshot = service.capture(user_id, domain=domain, target={'subjectId':subject_id})
            subject = conn.execute(select(t.subjects).where(t.subjects.c.id==subject_id,
                t.subjects.c.user_id==user_id)).mappings().one()
            if subject['status']=='archived':
                raise ApiError('NO_CHANGE', '이미 보관된 과목이에요', 400)
            tasks = [dict(row) for row in conn.execute(select(t.study_tasks).where(t.study_tasks.c.subject_id==subject_id,
                t.study_tasks.c.plan_id==snapshot.base_plan_id)).mappings()]
            task_ids = [task['id'] for task in tasks]
            sessions = [dict(row) for row in conn.execute(select(t.study_sessions).where(t.study_sessions.c.user_id==user_id,
                t.study_sessions.c.task_id.in_(task_ids))).mappings()]
            consumed = set(conn.scalars(select(t.study_records.c.session_id).where(
                t.study_records.c.task_id.in_(task_ids), t.study_records.c.session_id.is_not(None))))
            if any(row['status'] in ('running','paused') or (row['status'] in ('ended','auto_stopped') and row['id'] not in consumed)
                   for row in sessions):
                raise ApiError('ACTIVE_SESSION', '집중 세션을 종료하고 기록한 뒤 과목을 제거해 주세요', 409)
            records = set(conn.scalars(select(t.study_records.c.task_id).where(t.study_records.c.task_id.in_(task_ids))))
            ready = None
            for candidate in service.repository.ready(user_id, kind=domain.kind):
                if candidate['target'] != snapshot.target:
                    continue
                try:
                    service._require_fresh(candidate, snapshot)
                except ApiError:
                    continue
                ready = candidate['preview_response']
                break
            if ready is not None:
                return ready
            zone = ZoneInfo(conn.scalar(select(t.users.c.timezone).where(t.users.c.id==user_id)))
            slots, exclude = [], []
            for task in tasks:
                if task['scheduled_date'] < snapshot.base_date or task['status'] not in ('planned','missed') or task['id'] in records:
                    continue
                if task['task_type'] != 'remedial':
                    exclude.append(task['id'])
                if task['starts_at'] is not None and task['ends_at'] is not None:
                    start,end = _local(task['starts_at'],zone),_local(task['ends_at'],zone)
                    if start.date()==end.date() and start<end:
                        slots.append({'date':start.date().isoformat(),'start':start.strftime('%H:%M'),
                            'end':end.strftime('%H:%M'),'label':f"{subject['name']} 공부 시간"})
            response = {'subjectName':subject['name'], 'changes':[{'label':'과목 상태','before':'공부 중','after':'보관'}],
                'days':[{'date':slot['date'],'text':f"{slot['start']}–{slot['end']} 공부 시간을 비워요"} for slot in slots],
                'note':'학습·퀴즈 기록과 보충 의무의 원본은 보존해요'}
            return service.create(user_id, domain=domain, target=snapshot.target, snapshot=snapshot,
                normalized_input={'subjectId':subject_id}, changes={'subjectId':subject_id,'excludeIds':exclude,'slots':slots},
                preview_response=response, proposal_type='subject', description='과목 제거·학습 이력 보관')

    def apply(self, user_id, subject_id, preview_id):
        with self.engine.begin() as conn:
            existing = conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id==user_id)) is not None
            return ProposalService(conn).apply(user_id, preview_id, domain=DOMAINS[existing], target={'subjectId':subject_id})
