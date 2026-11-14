"""A review due-date owns one sealed quiz and one immutable API response."""
from uuid import NAMESPACE_URL, uuid5
from sqlalchemy import select
from fastapi.encoders import jsonable_encoder
from app.core.clock import now_utc, today_for
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.record_requests import RecordRequestRepository
from app.schemas.quiz import SubmitIn
from app.services.quiz import QuizService
from app.services.quiz_answers import lock_quiz_user
from app.services.quiz_finalization import QuizFinalizationService
from app.services.review_notes import ReviewNotesService


class _Finalizer(QuizFinalizationService):
    def __init__(self,review):
        super().__init__(review.engine)
        self.review=review

    def _finalize_locked(self,conn,user_id,admitted,now,view):
        result=super()._finalize_locked(conn,user_id,admitted,now,view)
        if result is not None:
            self.review.capture(conn,user_id,admitted.quiz_id,result)
        return result


class ReviewRetryService:
    def __init__(self,engine):
        self.engine=engine

    def receipt(self,conn,user_id,quiz_id,*,admission=False):
        return RecordRequestRepository(conn,user_id,'POST',self.path+('/admission' if admission else ''),quiz_id,self.payload)

    def capture(self,conn,user_id,quiz_id,result):
        receipt=self.receipt(conn,user_id,quiz_id)
        previous=receipt.replay()
        if previous is not None:return previous
        admission=self.receipt(conn,user_id,quiz_id,admission=True).replay()
        user=lock_quiz_user(conn,{'id':user_id})
        q,note=ReviewNotesService(conn,user,today_for(user)).note(self.question_id)
        item=result['items'][0]
        change=next((x for x in result['bookChanges'] if x['unitId']==q['unit_id']),None)
        unit=conn.execute(select(t.units).where(t.units.c.id==q['unit_id'])).mappings().one()
        response={'note':note,'correct':item['correct'],'confidence':item['confidence'],'score':item['score'],
            'explanation':item['explanation'],'wrongCountBefore':admission['wrongCountBefore'],
            'unitChange':{'materialId':q['material_id'],'unitId':q['unit_id'],'unitNo':note['unitNo'],
                'before':{k:change['before'][k] for k in ('score','grade')} if change else admission['before'],
                'after':{k:change['after'][k] for k in ('score','grade')} if change else
                    {'score':unit['mastery_score'],'grade':unit['mastery_level']}}}
        return receipt.save(jsonable_encoder(response))

    def admit(self,conn,user_id):
        user=lock_quiz_user(conn,{'id':user_id});today=today_for(user)
        q,note=ReviewNotesService(conn,user,today).note(self.question_id)
        pending=conn.execute(select(t.quiz_sessions)
            .join(t.quiz_items,t.quiz_sessions.c.id==t.quiz_items.c.quiz_session_id)
            .where(t.quiz_sessions.c.user_id==user_id,t.quiz_sessions.c.type=='review',t.quiz_items.c.question_id==self.question_id)
            .order_by(t.quiz_sessions.c.quiz_date.desc(),t.quiz_sessions.c.created_at.desc())).mappings().first()
        if pending and (pending['status'] in ('ready','in_progress') or not note['canRetry']):
            quiz_id=pending['id']
            try:
                response=self.receipt(conn,user_id,quiz_id).replay()
            except ApiError as error:
                if error.code=='IDEMPOTENCY_CONFLICT' and pending['status']=='submitted' and not note['canRetry']:
                    raise ApiError('REVIEW_NOT_DUE','아직 다음 복습 예정일이 아니에요',409) from None
                raise
            if response is not None:return quiz_id,response
            self.receipt(conn,user_id,quiz_id,admission=True).replay()  # conflict check before grading
            return quiz_id,None
        if not note['canRetry']:
            raise ApiError('REVIEW_NOT_DUE','아직 복습 예정일이 아니에요',409)
        quiz_id=str(uuid5(NAMESPACE_URL,f"team10:review:{user_id}:{self.question_id}:{q['next_review_date']}"))
        item_id=str(uuid5(NAMESPACE_URL,quiz_id+':item'))
        existing=conn.scalar(select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.id==quiz_id))
        if not existing:
            now=now_utc()
            conn.execute(t.quiz_sessions.insert().values(id=quiz_id,user_id=user_id,subject_id=q['subject_id'],
                type='review',quiz_date=q['next_review_date'],status='ready',selected_difficulty=q['difficulty'],created_at=now))
            conn.execute(t.quiz_items.insert().values(id=item_id,quiz_session_id=quiz_id,question_id=self.question_id,
                sort_order=1,inclusion_reason='weak'))
        # Validate before committing even the empty retry set.
        QuizService(conn,user,today)._validated_answers([{**q,'item_id':item_id}],
            [{'questionId':item_id,**self.payload}])
        receipt=self.receipt(conn,user_id,quiz_id,admission=True)
        if receipt.replay() is None:
            unit=conn.execute(select(t.units).where(t.units.c.id==q['unit_id'])).mappings().one()
            receipt.save(jsonable_encoder({'quizId':quiz_id,'wrongCountBefore':note['wrongCount'],
                'before':{'score':unit['mastery_score'],'grade':unit['mastery_level']}}))
        return quiz_id,self.receipt(conn,user_id,quiz_id).replay()

    def run(self,user_id,question_id,body):
        self.question_id=question_id;self.path=f'/api/review/notes/{question_id}/retry';self.payload=body.model_dump()
        with self.engine.begin() as conn:
            quiz_id,replay=self.admit(conn,user_id)
        if replay is not None:return replay
        item_id=str(uuid5(NAMESPACE_URL,quiz_id+':item'))
        result=_Finalizer(self).submit(user_id,quiz_id,SubmitIn.model_validate({'quizId':quiz_id,
            'answers':[{'questionId':item_id,**self.payload}]}))
        with self.engine.begin() as conn:
            lock_quiz_user(conn,{'id':user_id})
            return self.capture(conn,user_id,quiz_id,result)
