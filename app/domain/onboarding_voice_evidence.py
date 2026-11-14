"""Ground calendar writes in explicit local date, weekday and clock facts.

The model may choose a title/kind, but its confidence cannot supply missing
calendar coordinates. Unsupported or ambiguous expressions ask for clarification.
"""
from datetime import date, timedelta
import re

from app.core.responses import ApiError

_WEEKDAYS = {'월': 1, '화': 2, '수': 3, '목': 4, '금': 5, '토': 6, '일': 7}
_HOURS = {'한': 1, '두': 2, '세': 3, '네': 4, '다섯': 5, '여섯': 6,
          '일곱': 7, '여덟': 8, '아홉': 9, '열': 10, '열한': 11, '열두': 12}
_CLOCK = re.compile(
    r'(?P<period>오전|오후|아침|저녁|밤)?\s*'
    r'(?:(?<![\d:])(?P<colon_hour>\d{1,2}):(?P<colon_minute>\d{2})(?!\d)'
    r'|(?<![\d:열물])(?P<hour>\d{1,2}|열두|열한|다섯|여섯|일곱|여덟|아홉|한|두|세|네|열)\s*시'
    r'(?:\s*(?:(?P<minute>\d{1,2})\s*분|(?P<half>반)))?'
    r'|(?P<noon>정오)|(?P<midnight>자정))')
_NON_CREATION = re.compile(
    r'취소|삭제|지워|지우|제외|보류|미정|않|아니|아닌|'
    r'없(?:어|다|음|는|으|을|었|습)|'
    r'안\s*(?:해|하|할|잡|넣|추가|등록|저장|만들)|'
    r'(?:하지|하지는|넣지|잡지|만들지)\s*(?:마|말)|'
    r'(?:추가|등록|저장)\s*(?:금지|말고|말아)|빼\s*줘')
_CREATE_REQUEST = re.compile(r'추가|등록|저장|넣어|넣으|잡아|잡으|만들어|만드[는시]')
_EVENT_DESCRIPTION = re.compile(r'수업|약속|회의|근무|알바|점심|운동|병원|식사|모임|상담|면접|여행|출근|퇴근')


def _creation_intent(text, event):
    # An add endpoint may accept a definite event description, but never turn
    # cancellation, negation, a hypothetical, or coordinates alone into an add.
    if _NON_CREATION.search(text) or re.search(r'(?:있으|없으|되|하|가능하|된다|맞으)면|할까|될까', text):
        _fail()
    explicit_request = _CREATE_REQUEST.search(text) is not None
    if not explicit_request and re.search(r'\?|할까|될까|가능|어때|있나|인지|확인|알려', text):
        _fail()
    stated_title = len(event.title.strip()) >= 2 and event.title.strip() in text
    if not explicit_request and not _EVENT_DESCRIPTION.search(text) and not stated_title:
        _fail()


def _fail():
    raise ApiError('PARSE_FAILED', '추가할 일정과 날짜·요일, 시작·종료 시각을 명확히 알려 주세요', 400)


def _dates(text, today):
    found = []
    pattern = r'(?<!\d)(?:(\d{4})\s*(?:년\s*|[-/.]))?(\d{1,2})\s*(?:월\s*|[-/.])(\d{1,2})\s*일?(?!\d)'
    for match in re.finditer(pattern, text):
        year, month, day = match.groups()
        try:
            found.append(date(int(year) if year else today.year, int(month), int(day)))
        except ValueError:
            _fail()
    for token, delta in [('오늘', 0), ('내일', 1), ('모레', 2)]:
        if token in text:
            found.append(today + timedelta(days=delta))
    return set(found)


def _clocks(text):
    values, inherited_period = [], None
    for match in _CLOCK.finditer(text):
        period = match['period'] or inherited_period
        if match['period']:
            inherited_period = match['period']
        if match['noon'] or match['midnight']:
            hour, minute = (12 if match['noon'] else 0), 0
        else:
            raw = match['colon_hour'] or match['hour']
            hour = _HOURS[raw] if raw in _HOURS else int(raw)
            minute = int(match['colon_minute'] or match['minute'] or (30 if match['half'] else 0))
            if period:
                if not 1 <= hour <= 12:
                    _fail()
                hour = hour % 12 + (12 if period in ('오후', '저녁', '밤') else 0)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            _fail()
        values.append(f'{hour:02d}:{minute:02d}')
    return values


def validate_voice_evidence(text, event, *, today):
    """Reject inferred or contradictory coordinates before creating any event."""
    _creation_intent(text, event)
    if isinstance(today, str):
        today = date.fromisoformat(today)
    dates = _dates(text, today)
    weekdays = {_WEEKDAYS[day] for day in re.findall(r'([월화수목금토일])요일', text)}
    if event.date is not None:
        if dates != {event.date} or (weekdays and weekdays != {event.date.isoweekday()}):
            _fail()
        if '매주' in text or '매일' in text:
            _fail()
    else:
        if '매일' in text:
            weekdays = set(range(1, 8))
        if dates or weekdays != set(event.days):
            _fail()
    if _clocks(text) != [event.start, event.end]:
        _fail()
