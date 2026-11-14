"""Deterministic question-bank selection from owned, completed PDF ranges."""
from collections import defaultdict
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import PageSpan, intersect, normalize, page_count, subtract
from app.repositories.locking import lock_user, UserBusyError
from app.services.study import StudyData, Task


class QuizSelection:
    def __init__(self, conn, user, today):
        self.conn, self.user, self.today = conn, user, today
        self.data = StudyData(conn, user['id'])

    def lock(self):
        try:
            user = lock_user(self.conn, UUID(self.user['id']))
        except UserBusyError:
            raise ApiError('REQUEST_IN_PROGRESS', '퀴즈를 준비 중이에요', 409) from None
        if user is None:
            raise ApiError('NO_QUIZ', '퀴즈를 찾지 못했어요', 404)
        self.data = StudyData(self.conn, self.user['id'])

    def has_scope(self, subject_id):
        return any(self.data._task_exam_ranges(task, quiz_only=True)[0]
            for task in self.data.tasks_on(self.today) if task.row['subject_id']==subject_id)

    def completed(self, subject_id, today_only=True):
        if subject_id not in self.data.subject_by_id:
            raise ApiError('NO_QUIZ', '과목을 찾지 못했어요', 404)
        spans = defaultdict(list)
        tasks = self.data.tasks_on(self.today) if today_only else [*self.data.tasks, *self.planless_reviews(subject_id)]
        for task in tasks:
            if task.row['subject_id'] != subject_id or task.row['scheduled_date'] > self.today:
                continue
            for planned in task.paged:
                unit = self.data.units.get(planned['unit_id'])
                if unit is None or not unit['quiz_enabled'] or not unit['evidence']:
                    continue
                scope = intersect(self.data.unit_scopes[unit['id']],
                    [PageSpan(unit['material_id'], planned['start_page'], planned['end_page'])])
                done = [PageSpan(unit['material_id'], r['start_page'], r['end_page']) for r in task.done_ranges
                    if r['unit_id']==unit['id'] and r['start_page'] is not None and r['end_page'] is not None]
                spans[unit['id']].extend(intersect(scope, done))
        return {uid: normalize(parts) for uid,parts in spans.items() if parts}

    def planless_reviews(self, subject_id):
        """Direct relearning evidence is owned history, never a daily placement."""
        result=[]
        rows=self.conn.execute(select(t.study_tasks).join(t.subjects,t.subjects.c.id==t.study_tasks.c.subject_id)
            .where(t.subjects.c.user_id==self.user['id'],t.study_tasks.c.subject_id==subject_id,
                t.study_tasks.c.plan_id.is_(None),t.study_tasks.c.task_type=='review')).mappings()
        for row in rows:
            task=Task(dict(row))
            task.ranges=[dict(r) for r in self.conn.execute(select(t.study_task_ranges,t.units.c.material_id)
                .join(t.units,t.units.c.id==t.study_task_ranges.c.unit_id).join(t.materials,t.materials.c.id==t.units.c.material_id)
                .where(t.study_task_ranges.c.task_id==row['id'],t.materials.c.subject_id==subject_id)).mappings()]
            record=self.conn.execute(select(t.study_records).where(t.study_records.c.task_id==row['id'])
                .order_by(t.study_records.c.recorded_at.desc(),t.study_records.c.id.desc())).mappings().first()
            if record is not None and record['study_date']<=self.today:
                task.record=dict(record)
                task.done_ranges=[dict(r) for r in self.conn.execute(select(t.study_record_ranges)
                    .where(t.study_record_ranges.c.record_id==record['id'])).mappings()]
                result.append(task)
        return result

    def difficulty(self, unit_ids, requested):
        if requested is not None and requested not in ('low','mid','high'):
            raise ApiError('INVALID_INPUT', '난이도를 확인해 주세요', 422)
        ids = set(unit_ids)
        rounds = dict(self.conn.execute(select(t.learning_passes.c.unit_id, func.count(t.learning_passes.c.id))
            .where(t.learning_passes.c.unit_id.in_(ids), t.learning_passes.c.invalidated_at.is_(None))
            .group_by(t.learning_passes.c.unit_id)).all())
        if not ids or any(rounds.get(uid,0)<2 for uid in ids):
            return 'low'
        if requested is None:
            raise ApiError('INVALID_INPUT', '2회독부터는 난이도를 선택해 주세요', 422)
        return requested

    def questions(self, subject_id, scopes, difficulty=None):
        units = {uid:u for uid,u in self.data.units.items() if uid in scopes and u['subject_id']==subject_id and u['quiz_enabled'] and u['evidence']}
        materials = {r['id']:dict(r) for r in self.conn.execute(select(t.materials).where(t.materials.c.id.in_([u['material_id'] for u in units.values()]))).mappings()}
        concepts = {r['id']:dict(r) for r in self.conn.execute(select(t.concepts).where(t.concepts.c.unit_id.in_(units))).mappings()}
        questions = [dict(r) for r in self.conn.execute(select(t.questions).where(t.questions.c.unit_id.in_(units))
            .order_by(t.questions.c.code, t.questions.c.id)).mappings()]
        sources = defaultdict(list)
        for row in self.conn.execute(select(t.question_sources).where(t.question_sources.c.question_id.in_([q['id'] for q in questions]))).mappings():
            sources[row['question_id']].append(dict(row))
        valid = []
        for question in questions:
            uid = question['unit_id']; unit = units[uid]
            if difficulty is not None and question['difficulty'] != difficulty:
                continue
            if question['type'] not in ('choice','ox','short','essay') or not (question['prompt'] or '').strip() or not (question['explanation'] or '').strip() or not (question['correct_answer'] or '').strip():
                continue
            if question['concept_id'] is not None:
                concept = concepts.get(question['concept_id'])
                if concept is None or concept['unit_id'] != uid or not concept['in_scope'] or concept['retired_at'] is not None:
                    continue
            elif question['origin']=='generated':
                continue
            choices = question['choices']
            if question['type'] in ('choice','ox'):
                if not isinstance(choices,list) or len(choices)<2 or (question['type']=='ox' and len(choices)!=2):
                    continue
                if any(not isinstance(c,dict) or not isinstance(c.get('text'),str) or not c['text'].strip() for c in choices):
                    continue
                if sum(c['text']==question['correct_answer'] for c in choices)!=1:
                    continue
            elif choices:
                continue
            evidence = sources[question['id']]
            if not evidence:
                continue
            acceptable = True
            for source in evidence:
                try:
                    span = PageSpan(source['material_id'], source['start_page'], source['end_page'])
                except ValueError:
                    acceptable=False; break
                pages=materials[unit['material_id']]['page_count']
                if (source['material_id']!=unit['material_id'] or pages is None or span.end>pages or subtract([span], scopes[uid])
                    or not isinstance(source['quote'],str) or not source['quote'].strip()):
                    acceptable=False; break
            if acceptable:
                valid.append(question)
        return valid

    def assigned(self):
        return set(self.conn.scalars(select(t.quiz_items.c.question_id).join(t.quiz_sessions,
            t.quiz_sessions.c.id==t.quiz_items.c.quiz_session_id).where(t.quiz_sessions.c.user_id==self.user['id'])))

    def sessions(self, subject_id):
        return [dict(r) for r in self.conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.user_id==self.user['id'],
            t.quiz_sessions.c.subject_id==subject_id,t.quiz_sessions.c.type=='daily',t.quiz_sessions.c.quiz_date==self.today)
            .order_by(t.quiz_sessions.c.created_at,t.quiz_sessions.c.id)).mappings()]

    def fresh(self, subject_id, scopes, difficulty, sessions):
        covered=defaultdict(list)
        for row in self.conn.execute(select(t.quiz_scopes,t.units.c.material_id).join(t.units,t.units.c.id==t.quiz_scopes.c.unit_id)
            .where(t.quiz_scopes.c.quiz_session_id.in_([s['id'] for s in sessions]))).mappings():
            covered[row['unit_id']].append(PageSpan(row['material_id'],row['start_page'],row['end_page']))
        # Existing legacy sets lack scope snapshots; keep their already selected units stable.
        legacy_units=set(self.conn.scalars(select(t.questions.c.unit_id).join(t.quiz_items,t.quiz_items.c.question_id==t.questions.c.id)
            .where(t.quiz_items.c.quiz_session_id.in_([s['id'] for s in sessions]),t.quiz_items.c.inclusion_reason=='new')))
        remaining={uid:subtract(spans,covered[uid]) for uid,spans in scopes.items() if covered[uid] or uid not in legacy_units}
        remaining={uid:spans for uid,spans in remaining.items() if spans}
        pool=self.questions(subject_id,remaining,difficulty)
        used=self.assigned(); chosen=[]
        for uid in sorted(remaining):
            rows=[q for q in pool if q['unit_id']==uid and q['id'] not in used]
            if len(rows)<5:
                raise ApiError('NO_QUESTIONS','완료한 범위의 새 문항 5개가 아직 준비되지 않았어요',404)
            chosen.extend((q['id'],'new',None,None) for q in rows[:5])
        return chosen,remaining

    def reviews(self, subject_id):
        """Due weak questions and unanswered chain tips retain their source identity."""
        scope = self.completed(subject_id, today_only=False)
        eligible = {q['id'] for q in self.questions(subject_id, scope)}
        chosen = {}
        assigned_today = set(self.conn.scalars(select(t.quiz_items.c.question_id).join(t.quiz_sessions,
            t.quiz_sessions.c.id==t.quiz_items.c.quiz_session_id).where(t.quiz_sessions.c.user_id==self.user['id'],
            t.quiz_sessions.c.quiz_date==self.today, t.quiz_sessions.c.subject_id==subject_id)))
        descendants = select(t.quiz_items.c.origin_quiz_item_id).join(t.quiz_sessions,
            t.quiz_sessions.c.id==t.quiz_items.c.quiz_session_id).where(t.quiz_items.c.origin_quiz_item_id.is_not(None),
            t.quiz_sessions.c.user_id==self.user['id'])
        tips = self.conn.execute(select(t.quiz_items, t.quiz_sessions.c.quiz_date).join(t.quiz_sessions,
            t.quiz_sessions.c.id==t.quiz_items.c.quiz_session_id).outerjoin(t.quiz_attempts,
            t.quiz_attempts.c.quiz_item_id==t.quiz_items.c.id).outerjoin(t.quiz_answer_heads,
            t.quiz_answer_heads.c.quiz_item_id==t.quiz_items.c.id).where(t.quiz_sessions.c.user_id==self.user['id'],
            t.quiz_sessions.c.subject_id==subject_id,t.quiz_sessions.c.type=='daily',t.quiz_sessions.c.quiz_date<self.today,
            t.quiz_sessions.c.status.in_(('carried_over','submitted')),t.quiz_items.c.id.not_in(descendants),
            t.quiz_attempts.c.id.is_(None),t.quiz_answer_heads.c.answer_id.is_(None))
            .order_by(t.quiz_sessions.c.quiz_date,t.quiz_items.c.sort_order,t.quiz_items.c.id)).mappings()
        for row in tips:
            qid=row['question_id']
            if qid in eligible and qid not in assigned_today:
                chosen[qid]=(qid,'carried_over',row['id'],row['original_quiz_date'] or row['quiz_date'])
        weekend=set()
        if self.today.weekday() in (5,6):
            start=self.today-timedelta(days=5 if self.today.weekday()==5 else 1)
            end=self.today-timedelta(days=1)
            weekend=set(self.conn.scalars(select(t.quiz_items.c.question_id).join(t.quiz_sessions,
                t.quiz_sessions.c.id==t.quiz_items.c.quiz_session_id).join(t.quiz_attempts,
                t.quiz_attempts.c.quiz_item_id==t.quiz_items.c.id).where(t.quiz_sessions.c.user_id==self.user['id'],
                t.quiz_sessions.c.subject_id==subject_id,t.quiz_attempts.c.finalized_at.is_not(None),
                t.quiz_attempts.c.is_correct.is_(False),t.quiz_attempts.c.answered_date>=start,t.quiz_attempts.c.answered_date<=end)))
        weak=self.conn.execute(select(t.weak_questions).where(t.weak_questions.c.question_id.in_(eligible),
            t.weak_questions.c.status=='active').order_by(t.weak_questions.c.next_review_date,t.weak_questions.c.question_id)).mappings()
        for row in weak:
            qid=row['question_id']
            due=row['next_review_date'] is not None and row['next_review_date']<=self.today
            if qid not in assigned_today and (due or qid in weekend):
                chosen.setdefault(qid,(qid,'weak',None,None))
        return list(chosen.values())

    def create(self, subject_id, kind, chosen, difficulty, *, parent=None, scopes=None):
        identifier=str(uuid4())
        self.conn.execute(t.quiz_sessions.insert().values(id=identifier,user_id=self.user['id'],subject_id=subject_id,
            parent_quiz_session_id=parent,type=kind,quiz_date=self.today,status='ready',selected_difficulty=difficulty,
            grading_status='pending',current_position=0,created_at=now_utc()))
        self.append(identifier,chosen,scopes or {})
        return dict(self.conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.id==identifier)).mappings().one())

    def append(self, identifier, chosen, scopes):
        old=set(self.conn.scalars(select(t.quiz_items.c.question_id).where(t.quiz_items.c.quiz_session_id==identifier)))
        count=self.conn.scalar(select(func.max(t.quiz_items.c.sort_order)).where(t.quiz_items.c.quiz_session_id==identifier)) or 0
        for question,reason,origin,original_date in chosen:
            if question in old: continue
            count+=1;old.add(question)
            self.conn.execute(t.quiz_items.insert().values(id=str(uuid4()),quiz_session_id=identifier,question_id=question,
                sort_order=count,inclusion_reason=reason,origin_quiz_item_id=origin,original_quiz_date=original_date))
        for uid,spans in scopes.items():
            for span in spans:
                self.conn.execute(t.quiz_scopes.insert().values(id=str(uuid4()),quiz_session_id=identifier,unit_id=uid,
                    start_page=span.start,end_page=span.end))

    def ensure_daily(self, subject_id, requested=None):
        self.lock()
        if subject_id not in self.data.subject_by_id:
            raise ApiError('NO_QUIZ','과목을 찾지 못했어요',404)
        if not self.data.quiz_progress(self.today)['subjectOpen'].get(subject_id,False):
            raise ApiError('QUIZ_LOCKED','오늘의 출제 가능한 범위 70%를 완료하면 열려요',403)
        sessions=self.sessions(subject_id)
        current=next((s for s in sessions if s['status'] in ('ready','in_progress')),None)
        if current and requested is not None and current['selected_difficulty']!=requested:
            raise ApiError('QUIZ_DIFFICULTY_CONFLICT','이미 준비된 퀴즈의 난이도는 바꿀 수 없어요',409)
        if current and self.conn.scalar(select(t.quiz_submission_states.c.quiz_session_id).where(
                t.quiz_submission_states.c.quiz_session_id==current['id'])) is not None:
            return current
        scopes=self.completed(subject_id)
        difficulty=current['selected_difficulty'] if current else self.difficulty(scopes,requested)
        try:
            chosen,new_scopes=self.fresh(subject_id,scopes,difficulty,sessions)
        except ApiError as error:
            if error.code!='NO_QUESTIONS' or current is None: raise
            return current
        chosen = self.reviews(subject_id) + chosen
        if current:
            self.append(current['id'],chosen,new_scopes)
            return current
        if not chosen:
            done=[s for s in sessions if s['status']=='submitted']
            if done:return done[-1]
            raise ApiError('NO_QUESTIONS','완료한 범위의 문항이 아직 준비되지 않았어요',404)
        return self.create(subject_id,'daily',chosen,difficulty,scopes=new_scopes)
