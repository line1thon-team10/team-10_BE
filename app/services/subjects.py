"""Owned subject cards and draft creation; creating a subject never edits a plan."""
from collections import defaultdict
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.clock import now_utc, today_for
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.onboarding import subject_view
from app.domain.ranges import normalize, page_count
from app.repositories.locking import UserBusyError, lock_user
from app.services.study import StudyData


COLORS = ('#7974E8', '#ED986A', '#5BAA95', '#639FC7', '#CA7EAF', '#B39A56')


class SubjectService:
    def __init__(self, conn, user):
        self.conn, self.user = conn, user

    def create(self, name):
        name = name.strip()
        if not name:
            raise ApiError('EMPTY', '과목 이름을 입력해 주세요', 400)
        if '\x00' in name:
            raise ApiError('INVALID_INPUT', '과목 이름에 사용할 수 없는 문자가 있어요', 422)
        try:
            fresh = lock_user(self.conn, UUID(self.user['id']))
        except UserBusyError:
            raise ApiError('REQUEST_IN_PROGRESS', '다른 변경이 처리 중이에요. 다시 시도해 주세요', 409) from None
        if fresh is None:
            raise ApiError('UNKNOWN_SESSION', '사용자를 찾지 못했어요', 401)
        count = len(self.conn.scalars(select(t.subjects.c.id).where(t.subjects.c.user_id == self.user['id'])).all())
        subject_id, now = str(uuid4()), now_utc()
        self.conn.execute(t.subjects.insert().values(id=subject_id, user_id=self.user['id'],
            code='subject-' + subject_id, name=name, color=COLORS[count % len(COLORS)],
            status='draft', created_at=now, updated_at=now))
        return {'subjectId': subject_id, 'status': 'draft'}

    def listing(self):
        today = today_for(self.user)
        data = StudyData(self.conn, self.user['id'], subject_statuses=('draft', 'active', 'archived'))
        materials = defaultdict(list)
        for row in self.conn.execute(select(t.materials).join(t.subjects,
                t.materials.c.subject_id == t.subjects.c.id).where(t.subjects.c.user_id == self.user['id'])
                .order_by(t.materials.c.created_at, t.materials.c.id)).mappings():
            units = sorted((u for u in data.units.values() if u['material_id'] == row['id']),
                           key=lambda u: (u['sort_order'], u['id']))
            materials[row['subject_id']].append({'id': row['id'], 'name': row['original_name'],
                'pages': row['page_count'], 'toc': [{'unitId': u['id'], 'no': u['sort_order'],
                    'title': u['title'], 'from': u['start_page'], 'to': u['end_page']} for u in units]})
        cards = []
        for subject in data.subjects:
            goal = subject_view(subject)
            aggregate = data.readiness(subject)
            spans = normalize(span for uid, unit in data.units.items() if unit['subject_id'] == subject['id']
                              for span in data.unit_scopes[uid])
            single = spans[0] if len(spans) == 1 else None
            cards.append({'id': subject['id'], 'name': subject['name'], 'color': subject['color'],
                'status': subject['status'], 'examDate': goal['examDate'],
                'dday': (subject['exam_date'] - today).days if subject['exam_date'] else None,
                'goal': goal['goal'], 'goalTotal': goal['goalTotal'], 'target': goal['target'],
                'range': {'from': single.start if single else None, 'to': single.end if single else None,
                          'pages': page_count(spans)},
                'progress': {'studied': aggregate['studiedPages'], 'total': aggregate['totalPages'],
                             'rate': aggregate['progressRate']},
                'readiness': aggregate['readiness'], 'removed': subject['archive_reason'] == 'user_removed',
                'archived': subject['status'] == 'archived', 'materials': materials[subject['id']]})
        return {'today': today.isoformat(), 'subjects': cards}
