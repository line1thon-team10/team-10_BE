"""Exactly five unseen questions with one durable extra set per unit and day."""
from sqlalchemy import select
from app.core.responses import ApiError
from app.db import schema as t
from app.services.quiz_selection import QuizSelection


def create_extra(conn, user, today, subject_id, unit_id, requested=None):
    selection=QuizSelection(conn,user,today)
    selection.lock()
    unit=selection.data.units.get(unit_id)
    if unit is None or unit['subject_id']!=subject_id:
        raise ApiError('UNIT_NOT_FOUND','단원을 찾지 못했어요',404)
    existing=conn.execute(select(t.quiz_sessions).join(t.quiz_scopes,t.quiz_scopes.c.quiz_session_id==t.quiz_sessions.c.id)
        .where(t.quiz_sessions.c.user_id==user['id'],t.quiz_sessions.c.subject_id==subject_id,
            t.quiz_sessions.c.type=='extra',t.quiz_sessions.c.quiz_date==today,t.quiz_scopes.c.unit_id==unit_id)
        .order_by(t.quiz_sessions.c.created_at,t.quiz_sessions.c.id)).mappings().first()
    if existing:
        if requested is not None and requested!=existing['selected_difficulty']:
            raise ApiError('QUIZ_DIFFICULTY_CONFLICT','이미 준비된 퀴즈의 난이도는 바꿀 수 없어요',409)
        return {'quizId':existing['id'],'count':5}
    base=conn.execute(select(t.quiz_sessions).join(t.quiz_items,t.quiz_items.c.quiz_session_id==t.quiz_sessions.c.id)
        .join(t.questions,t.questions.c.id==t.quiz_items.c.question_id)
        .join(t.quiz_submission_states,t.quiz_submission_states.c.quiz_session_id==t.quiz_sessions.c.id)
        .where(t.quiz_sessions.c.user_id==user['id'],t.quiz_sessions.c.subject_id==subject_id,
            t.quiz_sessions.c.type=='daily',t.quiz_sessions.c.quiz_date==today,
            t.quiz_sessions.c.status=='submitted',t.quiz_sessions.c.grading_status=='completed',
            t.quiz_sessions.c.finalized_at.is_not(None),t.quiz_submission_states.c.result_snapshot.is_not(None),
            t.questions.c.unit_id==unit_id)
        .order_by(t.quiz_sessions.c.created_at.desc(),t.quiz_sessions.c.id.desc())).mappings().first()
    if base is None:
        raise ApiError('NO_RESULT','이 단원이 포함된 오늘 퀴즈 결과를 먼저 확인해 주세요',404)
    scope=selection.completed(subject_id,today_only=False)
    scope={unit_id:scope[unit_id]} if unit_id in scope else {}
    difficulty=selection.difficulty([unit_id],requested)
    used=selection.assigned()
    pool=[q for q in selection.questions(subject_id,scope,difficulty) if q['id'] not in used]
    if len(pool)<5:
        raise ApiError('NO_QUESTIONS','완료한 범위의 더 풀기 문항 5개가 아직 준비되지 않았어요',404)
    quiz=selection.create(subject_id,'extra',[(q['id'],'extra',None,None) for q in pool[:5]],difficulty,
        parent=base['id'],scopes=scope)
    return {'quizId':quiz['id'],'count':5}
