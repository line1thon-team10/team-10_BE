"""Three immutable choices for actual time released by approved changes."""
from collections import defaultdict
from datetime import date
from fractions import Fraction
from uuid import uuid4

from sqlalchemy import select

from app.core.clock import today_for
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import PageSpan, subtract
from app.domain.scheduling import MULTIPLIERS, Slot, half_up
from app.repositories.proposals import ProposalRepository, ProposalNotFound, LegacyProposalUnavailable, LegacyResponseUnavailable
from app.services.plan_draft import _basis
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from app.services.schedule_placements import apply_saved_placements, clock_text, free_calendar_slots, move_task
from app.services.study import StudyData


def _apply_for(option):
    def apply(conn,context,changes):
        candidate=changes['choices'].get(option)
        if candidate is None:
            raise ApiError('OPTION_DISABLED','이 빈 시간에는 선택할 수 없는 방법이에요',409)
        apply_saved_placements(conn,context,candidate)
        return {'applied':True,'selectedOption':option}
    return apply


def _domain(option):
    return PreviewDomain('empty_time',PlanEffect.UPDATE,_basis,_apply_for(option))


class EmptyTimeService:
    def __init__(self,conn,user):
        self.conn,self.user=conn,user

    def offer(self,slots):
        """Call after the parent's revision update, under its transaction/user lock."""
        service=ProposalService(self.conn)
        target={'source':'released_time','offerId':str(uuid4())}
        snapshot=service.capture(self.user['id'],domain=_domain('rest'),target=target)
        available=free_calendar_slots(self.conn,self.user,slots)
        actual=[{'date':s.day.isoformat(),'start':clock_text(s.start),'end':clock_text(s.end),'label':'비워진 시간'} for s in available]
        if not actual:
            return {'emptyTime':None,'slots':[],'options':[],'previewId':None}
        data=StudyData(self.conn,self.user['id'])
        choices={'rest':{'placements':[],'supersedeIds':[]},'pull':self._pull(data,available),'advance':self._advance(data,available)}
        labels={'rest':('쉬기','비워진 시간을 그대로 둬요'), 'pull':('앞당겨 공부하기','기록 없는 미래 태스크를 앞당겨요'),
                'advance':('다음 범위 공부하기','아직 배정되지 않은 새 범위를 공부해요')}
        options=[{'key':key,'title':labels[key][0],'desc':labels[key][1],'enabled':value is not None,
            'reason':None if value is not None else ('들어갈 수 있는 미래 태스크가 없어요' if key=='pull' else '시험 전 빈 시간에 배정할 다음 범위가 없어요'),
            'default':key=='rest'} for key,value in choices.items()]
        response={'emptyTime':actual[0],'slots':actual,'options':options}
        return service.create(self.user['id'],domain=_domain('rest'),target=target,snapshot=snapshot,
            normalized_input={'slots':actual},changes={'choices':choices},preview_response=response,
            proposal_type='empty_time',description='비워진 시간의 공부 또는 휴식 선택')

    def _pull(self,data,slots):
        session_tasks=set(self.conn.scalars(select(t.study_sessions.c.task_id).where(t.study_sessions.c.user_id==self.user['id'])))
        candidates=[task for task in data.tasks if task.row['status']=='planned' and task.record is None
            and task.id not in session_tasks and task.row['subject_id'] in data.subject_by_id]
        candidates.sort(key=lambda task:(task.row['scheduled_date'],task.id))
        placements,moved=[],set()
        for slot in slots:
            cursor=slot.start
            for task in candidates:
                subject=data.subject_by_id[task.row['subject_id']]
                duration=Fraction(task.row['estimated_minutes'])
                if (task.id in moved or task.row['scheduled_date']<=slot.day or not subject['exam_date']
                    or slot.day>=subject['exam_date'] or not 15<=duration<=60 or cursor+duration>slot.end):
                    continue
                placements.append(move_task(task,Slot(slot.day,cursor,slot.end)))
                moved.add(task.id);cursor+=duration
        return {'placements':placements,'supersedeIds':sorted(moved)} if placements else None

    def _advance(self,data,slots):
        used=defaultdict(list)
        for task in data.tasks:
            if task.row['task_type']!='study': continue
            by_unit={part['unit_id']:part['material_id'] for part in task.ranges}
            for part in task.done_ranges:
                if part['unit_id'] in by_unit and part['start_page'] is not None and part['end_page'] is not None:
                    used[by_unit[part['unit_id']]].append(PageSpan(by_unit[part['unit_id']],part['start_page'],part['end_page']))
            if task.row['status'] not in ('carried_over','excluded','missed'):
                for part in task.paged:
                    used[part['material_id']].append(PageSpan(part['material_id'],part['start_page'],part['end_page']))
        units=sorted(data.units.values(),key=lambda unit:(data.subject_by_id[unit['subject_id']]['exam_date'] or date.max,
            unit['material_id'],unit['sort_order'],unit['id']))
        placements=[]
        for slot in slots:
            cursor=slot.start
            for unit in units:
                subject=data.subject_by_id[unit['subject_id']]
                if subject['exam_date'] is None or slot.day>=subject['exam_date']: continue
                pace=Fraction(str(subject['minutes_per_page']))*MULTIPLIERS[unit['difficulty']]
                for span in subtract(data.unit_scopes[unit['id']],used[unit['material_id']]):
                    start=span.start
                    while start<=span.end:
                        count=min(span.end-start+1,int(min(Fraction(60),slot.end-cursor)//pace))
                        minutes=count*pace
                        if count<=0 or minutes<15: break
                        finish=cursor+minutes
                        placements.append({'id':str(uuid4()),'subjectId':subject['id'],'blockId':None,'originTaskId':None,
                            'kind':'study','title':unit['title'],'date':slot.day.isoformat(),
                            'start':[cursor.numerator,cursor.denominator],'end':[finish.numerator,finish.denominator],
                            'minutes':half_up(minutes),'ranges':[{'unit_id':unit['id'],'concept_id':None,'start_page':start,
                                'end_page':start+count-1,'planned_minutes':half_up(minutes),'sort_order':1}]})
                        used[unit['material_id']].append(PageSpan(unit['material_id'],start,start+count-1))
                        cursor=finish;start+=count
        return {'placements':placements,'supersedeIds':[]} if placements else None

    def apply(self,preview_id,option):
        try:
            candidate=ProposalRepository(self.conn).get(self.user['id'],str(preview_id))
        except ProposalNotFound as error:
            raise ApiError('NO_PREVIEW','빈 시간 후보를 찾지 못했어요',404 if error.foreign else 400) from None
        except (LegacyProposalUnavailable,LegacyResponseUnavailable):
            raise ApiError('PREVIEW_STALE','과거 후보를 다시 확인해 주세요',409) from None
        if candidate['kind']!='empty_time':
            raise ApiError('NO_PREVIEW','빈 시간 후보가 아니에요',404)
        if candidate['status']=='applied' and candidate['result_response'].get('selectedOption')!=option:
            raise ApiError('PREVIEW_STALE','이미 다른 선택을 적용한 후보예요',409)
        result=ProposalService(self.conn).apply(self.user['id'],preview_id,domain=_domain(option),target=candidate['target'])
        return {'applied':result['applied']}
