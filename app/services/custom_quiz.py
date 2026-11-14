"""Validated practice scopes with deterministic fair or weak-first selection."""
from collections import defaultdict
from uuid import uuid4
from sqlalchemy import select,update
from app.core.clock import today_for
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import PageSpan,intersect,subtract
from app.repositories.record_requests import RecordRequestRepository
from app.services.quiz_answers import lock_quiz_user
from app.services.quiz_selection import QuizSelection


class CustomQuizService:
    def __init__(self,conn,user):
        self.conn,self.user=conn,user

    def create(self,body,key=None):
        self.user=lock_quiz_user(self.conn,self.user)
        receipt=RecordRequestRepository(self.conn,self.user['id'],'POST','/api/review/custom-quiz',key,body.model_dump(mode='json',by_alias=True))
        replay=receipt.replay()
        if replay is not None:return replay
        selector=QuizSelection(self.conn,self.user,today_for(self.user));subject_id=str(body.subjectId)
        if subject_id not in selector.data.subject_by_id:
            raise ApiError('SUBJECT_NOT_FOUND','과목을 찾지 못했어요',404)
        completed=selector.completed(subject_id,today_only=False)
        seen=set();scopes=[];units={}
        for scope in body.scopes:
            uid,cid=str(scope.unitId),str(scope.conceptId) if scope.conceptId else None
            identity=(uid,cid,scope.from_page,scope.to)
            if identity in seen:raise ApiError('INVALID_INPUT','선택 범위가 중복돼요',422)
            seen.add(identity)
            unit=selector.data.units.get(uid)
            if unit is None or unit['subject_id']!=subject_id:
                raise ApiError('NO_UNITS','단원을 찾지 못했어요',404)
            if not unit['quiz_enabled']:
                raise ApiError('INVALID_INPUT','출제할 수 없는 자료예요',422)
            effective=(PageSpan(unit['material_id'],scope.from_page,scope.to),)
            if cid is not None:
                concept=self.conn.execute(select(t.concepts).where(t.concepts.c.id==cid,t.concepts.c.unit_id==uid,
                    t.concepts.c.retired_at.is_(None),t.concepts.c.in_scope.is_(True))).mappings().first()
                if concept is None or concept['start_page'] is None or concept['end_page'] is None:
                    raise ApiError('INVALID_INPUT','선택 개념의 시험 범위를 확인해 주세요',422)
                effective=intersect(effective,[PageSpan(unit['material_id'],concept['start_page'],concept['end_page'])])
            if not effective or subtract(effective,selector.data.unit_scopes[uid]) or subtract(effective,completed.get(uid,())):
                raise ApiError('INVALID_INPUT','완료한 시험 범위만 선택할 수 있어요',422)
            if any(old_uid==uid and old_cid==cid and intersect(effective,old_parts) for old_uid,old_cid,old_parts in scopes):
                raise ApiError('INVALID_INPUT','선택 범위가 겹쳐요',422)
            scopes.append((uid,cid,effective));units[uid]=unit
        difficulty=selector.difficulty(units,body.difficulty)
        available={}
        for uid,cid,parts in scopes:
            for question in selector.questions(subject_id,{uid:parts},difficulty):
                if cid is None or question['concept_id']==cid:available[question['id']]=question
        attempted=set(self.conn.scalars(select(t.quiz_items.c.question_id).join(t.quiz_attempts,t.quiz_attempts.c.quiz_item_id==t.quiz_items.c.id)
            .join(t.quiz_sessions,t.quiz_sessions.c.id==t.quiz_items.c.quiz_session_id)
            .where(t.quiz_sessions.c.user_id==self.user['id'],t.quiz_attempts.c.finalized_at.is_not(None))))
        attempted.update(self.conn.scalars(select(t.quiz_items.c.question_id)
            .join(t.quiz_answer_heads,t.quiz_answer_heads.c.quiz_item_id==t.quiz_items.c.id)
            .join(t.quiz_sessions,t.quiz_sessions.c.id==t.quiz_items.c.quiz_session_id)
            .where(t.quiz_sessions.c.user_id==self.user['id'])))
        buckets=defaultdict(list)
        for question in sorted(available.values(),key=lambda q:(q['id'] in attempted,q['code'] or '',q['id'])):
            buckets[question['unit_id']].append(question['id'])
        order=sorted(units,key=lambda uid:((float(units[uid]['mastery_score']) if units[uid]['mastery_score'] is not None else -1)
            if body.weight=='weak' else 0,units[uid]['sort_order'],uid))
        chosen=[]
        if body.weight=='weak':
            chosen=[qid for uid in order for qid in buckets[uid]][:body.count]
        else:
            while len(chosen)<body.count and any(buckets.values()):
                for uid in order:
                    if buckets[uid] and len(chosen)<body.count:chosen.append(buckets[uid].pop(0))
        if not chosen:raise ApiError('NO_QUESTIONS','선택한 범위의 문항이 아직 준비되지 않았어요',404)
        quiz=selector.create(subject_id,'practice',[(qid,'practice',None,None) for qid in chosen],difficulty)
        self.conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==quiz['id']).values(
            requested_question_count=body.count,distribution='balanced' if body.weight=='even' else 'weak_first'))
        for uid,cid,parts in scopes:
            for span in parts:self.conn.execute(t.quiz_scopes.insert().values(id=str(uuid4()),quiz_session_id=quiz['id'],
                unit_id=uid,concept_id=cid,start_page=span.start,end_page=span.end))
        notes=[]
        if difficulty!=body.difficulty:notes.append('1회독 단원이 포함되어 난이도를 하로 고정했어요.')
        if len(chosen)<body.count:notes.append(f'준비된 문항 {len(chosen)}개로 구성했어요.')
        return receipt.save({'quizId':quiz['id'],'count':len(chosen),'difficulty':difficulty,'note':' '.join(notes) or None})
