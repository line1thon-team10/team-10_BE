"""Read-only weak-question history, scoped through both question and quiz owner."""
from collections import defaultdict
from sqlalchemy import select
from app.core.responses import ApiError
from app.db import schema as t
from app.services.task_views import unit_no


def result_name(attempt):
    return ('sure' if attempt['confidence']=='sure' else 'unsure') + ('Right' if attempt['is_correct'] else 'Wrong')


class ReviewNotesService:
    def __init__(self, conn, user, today):
        self.conn, self.user, self.today = conn, user, today

    def questions(self):
        return [dict(row) for row in self.conn.execute(select(t.questions,
            t.units.c.material_id, t.units.c.code.label('unit_code'), t.units.c.title.label('unit_title'),
            t.units.c.sort_order.label('unit_order'), t.materials.c.subject_id,
            t.subjects.c.name.label('subject_name'), t.concepts.c.title.label('concept_title'),
            t.weak_questions.c.status.label('weak_status'), t.weak_questions.c.review_step, t.weak_questions.c.next_review_date)
            .join(t.units,t.questions.c.unit_id==t.units.c.id)
            .join(t.materials,t.units.c.material_id==t.materials.c.id)
            .join(t.subjects,t.materials.c.subject_id==t.subjects.c.id)
            .join(t.weak_questions,t.questions.c.id==t.weak_questions.c.question_id)
            .outerjoin(t.concepts,(t.questions.c.concept_id==t.concepts.c.id)&(t.concepts.c.unit_id==t.units.c.id))
            .where(t.subjects.c.user_id==self.user['id'],
                t.questions.c.concept_id.is_(None)|t.concepts.c.id.is_not(None))).mappings()]

    def histories(self, ids):
        grouped=defaultdict(list)
        rows=self.conn.execute(select(t.quiz_attempts,t.quiz_items.c.question_id)
            .join(t.quiz_items,t.quiz_attempts.c.quiz_item_id==t.quiz_items.c.id)
            .join(t.quiz_sessions,t.quiz_items.c.quiz_session_id==t.quiz_sessions.c.id)
            .join(t.questions,t.quiz_items.c.question_id==t.questions.c.id)
            .join(t.units,t.questions.c.unit_id==t.units.c.id)
            .join(t.materials,t.units.c.material_id==t.materials.c.id)
            .join(t.subjects,t.materials.c.subject_id==t.subjects.c.id)
            .where(t.quiz_items.c.question_id.in_(ids),t.subjects.c.user_id==self.user['id'],
                t.quiz_sessions.c.user_id==self.user['id'],
                t.quiz_sessions.c.subject_id.is_(None)|(t.quiz_sessions.c.subject_id==t.materials.c.subject_id),
                t.quiz_sessions.c.grading_status=='completed',t.quiz_attempts.c.answered_date<=self.today,
                t.quiz_attempts.c.finalized_at.is_not(None),
                t.quiz_attempts.c.graded_at.is_not(None),t.quiz_attempts.c.is_correct.is_not(None),
                t.quiz_attempts.c.grading_status.in_(('graded','fallback')),
                t.quiz_sessions.c.status.in_(('submitted','carried_over')),
                t.quiz_sessions.c.finalized_at.is_not(None))
            .order_by(t.quiz_attempts.c.answered_date,t.quiz_attempts.c.answered_at,t.quiz_attempts.c.id)).mappings()
        for row in rows: grouped[row['question_id']].append(dict(row))
        return grouped

    def _notes(self):
        questions=self.questions();histories=self.histories([q['id'] for q in questions])
        sources={}
        for row in self.conn.execute(select(t.question_sources).where(t.question_sources.c.question_id.in_(list(histories)))
            .order_by(t.question_sources.c.start_page,t.question_sources.c.id)).mappings():
            sources.setdefault((row['question_id'],row['material_id']),row['start_page'])
        notes=[]
        for q in questions:
            history=histories[q['id']]
            weak=[a for a in history if not a['is_correct'] or a['confidence']=='unsure']
            if not weak: continue
            last=history[-1]
            note={'id':q['id'],'subjectId':q['subject_id'],'subjectName':q['subject_name'],
                'materialId':q['material_id'],'unitId':q['unit_id'],'conceptId':q['concept_id'],
                'unitNo':unit_no(q['unit_code']),'unitTitle':q['unit_title'],'concept':q['concept_title'],
                'question':q['prompt'],'page':sources.get((q['id'],q['material_id'])),'type':q['type'],
                'wrongCount':sum(not a['is_correct'] for a in history),'lastDate':last['answered_date'].isoformat(),
                'result':'resolved' if q['weak_status']=='resolved' else result_name(weak[-1]),
                'score':last['essay_score'] if q['type']=='essay' else None,'streak':q['review_step'],
                'canRetry':q['weak_status']=='active' and q['next_review_date'] is not None and self.today>=q['next_review_date'],
                'history':[{'date':a['answered_date'].isoformat(),'result':result_name(a),
                    'score':a['essay_score'] if q['type']=='essay' else None} for a in history], 'mockFilled':None}
            notes.append((q,note))
        return notes

    def note(self, question_id):
        for q,note in self._notes():
            if q['id']==question_id: return q,note
        raise ApiError('NO_QUESTION','복습 문항을 찾지 못했어요',404)

    def question(self, question_id):
        q,_=self.note(question_id)
        return {'noteId':q['id'],'type':'choice' if q['type'] in ('choice','ox') else q['type'],
            'prompt':q['prompt'],'options':[c['text'] for c in q['choices']] if q['choices'] else [],'mockFilled':None}

    def grouped(self, group):
        entries=self._notes()
        if group=='date': entries.sort(key=lambda x:(-int(x[1]['lastDate'].replace('-','')),x[0]['subject_id'],x[0]['unit_order'],x[0]['id']))
        elif group=='unit': entries.sort(key=lambda x:(x[0]['subject_id'],x[0]['unit_order'],x[0]['unit_id'],x[0]['id']))
        else: entries.sort(key=lambda x:(-x[1]['wrongCount'],x[0]['subject_id'],x[0]['unit_order'],x[0]['id']))
        groups={};concepts={}
        for q,note in entries:
            key=note['lastDate'] if group=='date' else q['unit_id'] if group=='unit' else str(note['wrongCount'])
            label=key if group=='date' else f"{q['subject_name']} · {q['unit_title']}" if group=='unit' else f"{key}회"
            groups.setdefault(key,{'key':key,'label':label,'items':[]})['items'].append(note)
            if q['concept_id'] is not None and q['concept_title'] is not None:
                c=concepts.setdefault(q['concept_id'],{'subjectId':q['subject_id'],'subjectName':q['subject_name'],
                    'materialId':q['material_id'],'unitId':q['unit_id'],'conceptId':q['concept_id'],
                    'concept':q['concept_title'],'count':0})
                c['count']+=note['wrongCount']
        return {'group':group,'total':len(entries),'groups':list(groups.values()),
            'weakConcepts':sorted((v for v in concepts.values() if v['count']>=2),key=lambda x:(-x['count'],x['subjectId'],x['unitId'],x['conceptId']))}
