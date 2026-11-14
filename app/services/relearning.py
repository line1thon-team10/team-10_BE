"""Explicit full-scope completion creates auditable, out-of-plan page evidence."""
import hashlib
from uuid import uuid4
from sqlalchemy import func,select
from app.core.clock import now_utc,today_for
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.record_requests import RecordRequestRepository
from app.services.quiz_answers import lock_quiz_user
from app.services.learning_passes import scope_revision
from app.services.study import StudyData
from app.services.task_views import unit_no


class RelearningService:
    def __init__(self,conn,user):self.conn,self.user=conn,user

    def complete(self,body,key):
        if key is None:raise ApiError('INVALID_INPUT','직접 재학습에는 Idempotency-Key가 필요해요',422)
        self.user=lock_quiz_user(self.conn,self.user)
        receipt=RecordRequestRepository(self.conn,self.user['id'],'POST','/api/review/relearn',key,body.model_dump(mode='json'))
        replay=receipt.replay()
        if replay is not None:return replay
        unit=self.conn.execute(select(t.units,t.materials.c.subject_id)
            .join(t.materials,t.materials.c.id==t.units.c.material_id)
            .join(t.subjects,t.subjects.c.id==t.materials.c.subject_id)
            .where(t.units.c.id==str(body.unitId),t.subjects.c.id==str(body.subjectId),t.subjects.c.user_id==self.user['id'],
                t.subjects.c.status=='active',t.units.c.retired_at.is_(None))).mappings().first()
        if unit is None:raise ApiError('NO_UNIT','재학습할 단원을 찾지 못했어요',404)
        concepts=self.conn.execute(select(t.concepts).where(t.concepts.c.unit_id==unit['id'],t.concepts.c.retired_at.is_(None))).mappings().all()
        required=StudyData._unit_scope(unit,concepts)
        if unit['quantity_basis']!='page' or not required:
            raise ApiError('INVALID_INPUT','현재 시험 범위의 페이지가 있는 단원만 완료할 수 있어요',422)
        today,at=today_for(self.user),now_utc()
        if self.conn.scalar(select(t.learning_passes.c.id).where(t.learning_passes.c.unit_id==unit['id'],
            t.learning_passes.c.completion_kind=='full_relearn',t.learning_passes.c.completed_date==today)):
            raise ApiError('ALREADY','오늘 이 단원의 재학습을 이미 기록했어요',409)
        task_id,block_id,record_id=(str(uuid4()) for _ in range(3))
        self.conn.execute(t.study_blocks.insert().values(id=block_id,subject_id=unit['subject_id'],unit_id=unit['id'],
            task_type='review',stacked_at=at,created_at=at))
        self.conn.execute(t.study_tasks.insert().values(id=task_id,plan_id=None,subject_id=unit['subject_id'],block_id=block_id,
            task_type='review',title=unit['title'],scheduled_date=today,estimated_minutes=0,status='completed',
            placement_reason='명시적 전체 재학습 완료',created_at=at))
        self.conn.execute(t.study_records.insert().values(id=record_id,task_id=task_id,result='completed',
            recorded_at=at,study_date=today,updated_at=at))
        for order,part in enumerate(required):
            self.conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=task_id,unit_id=unit['id'],
                start_page=part.start,end_page=part.end,sort_order=order))
            self.conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record_id,unit_id=unit['id'],
                start_page=part.start,end_page=part.end))
        number=1+(self.conn.scalar(select(func.max(t.learning_passes.c.pass_number)).where(t.learning_passes.c.unit_id==unit['id'])) or 0)
        snapshot={'schemaVersion':'1','materialId':unit['material_id'],'recordIds':[record_id],'sessionIds':[],
            'requiredRanges':[[part.start,part.end] for part in required],
            'masteryScopeRevision':scope_revision(self.conn,unit),'directRequestHash':receipt.request_hash}
        self.conn.execute(t.learning_passes.insert().values(id=str(uuid4()),unit_id=unit['id'],pass_number=number,
            completion_kind='full_relearn',source_task_id=task_id,evidence_key='direct:'+hashlib.sha256(key.encode()).hexdigest(),
            evidence_snapshot=snapshot,completed_at=at,completed_date=today))
        count=self.conn.scalar(select(func.count()).select_from(t.learning_passes).where(t.learning_passes.c.unit_id==unit['id'],
            t.learning_passes.c.invalidated_at.is_(None)))
        return receipt.save({'subjectId':unit['subject_id'],'materialId':unit['material_id'],'unitId':unit['id'],
            'unitNo':unit_no(unit['code']),'round':count,'relearnedOn':today.isoformat()})
