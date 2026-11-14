"""Source-grounded scheduling intent; never generates or mutates a study plan."""
from datetime import date, timedelta
import json
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.ai.gateway import FunctionRef, InvalidOutput, ProviderSpec, canonical_json
from app.ai.material_adapter import ObservedGroq

MODEL_VERSION = 'openai/gpt-oss-120b'
PROMPT_VERSION = 'resplit-grounded-windows-v1'
SCHEMA_VERSION = 'resplit-intent-v1'
PROVIDER = ProviderSpec(FunctionRef(__name__, 'resplit_provider'))
VALIDATOR = FunctionRef(__name__, 'validate_resplit_result')


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Question(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    choices: list[str] = Field(max_length=8)


class Window(StrictModel):
    subjectId: str | None
    dates: list[str] = Field(min_length=1, max_length=3660)
    minutesPerDay: int | None = Field(ge=1, le=1440)


class Intent(StrictModel):
    ready: bool
    question: Question | None
    windows: list[Window] = Field(max_length=100)


_WEEKDAYS = '월화수목금토일'
_DATE = re.compile(r'(?<![\d.\-])(?:(\d{4})-(\d{2})-(\d{2})|(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일|(\d{1,2})월\s*(\d{1,2})일)(?!\d)')
_GENERIC = re.compile(r'(?:남은\s*전체\s*범위를?\s*)?(?:다시\s*(?:배분|재배분|나눠|분배)(?:해)?(?:줘|주세요)|재생성(?:해줘|해주세요)?)|현재\s*일정과\s*목표대로\s*재생성(?:해줘|해주세요)?')
_CAP = re.compile(r'(?:하루|매일|일일)\s*(\d+)\s*(분|시간)(?:\s*(\d+)\s*분)?|(?<!\d)(\d+)\s*(분|시간)\s*씩')
_UNCERTAIN = re.compile(r'아마|정도|대충|쯤|가능하면|혹시|않|아니|안\s|못|말고|제외|빼|취소|대신|이상|최소|보다|[?？]|거나|든지|할까|하면|일지도|또는|혹은')
# Everything not consumed as an owned selector or explicit constraint must be a
# scheduling request connector. Unknown clauses must not silently disappear.
_FILLER = re.compile(r'전체|모든|과목|공부|학습|계획|일정|시간|분량|배정|배분|재배분|재분배|재분할|재생성|나눠|다시|남은|새로|수립|만들어|해줘|해주세요|해\s*주세요|해|줘|주세요|하고|하자|하도록|하겠어|할게|하고싶어|하고\s*싶어|부탁해|부탁|변경|조정|제한|최대|이하|이내|상한|까지|부터|동안|매주|요일|매일|에만|만|에는|에|은|는|을|를|로|으로|와|과|및|또는|하고|씩|도|의|총|[\s,·/\-~〜～.!]')


def _subject_pattern(name):
    return re.compile(r'(?<![\w])' + re.escape(name) + r'(?=$|\s|[,;:/]|(?:은|는|을|를|만|도|에|과|와)(?:\s|$))')


def _days(left, right):
    if not 0 <= (right - left).days <= 3659:
        raise ValueError('Unsupported or expired planning horizon')
    return [left + timedelta(days=i) for i in range((right - left).days + 1)]


def _clause_window(text, context):
    today = date.fromisoformat(context['today'])
    subjects = context['subjects']
    selected = []
    selector_positions = []
    # Longest names first prevents a short subject name from shadowing another.
    for subject in sorted(subjects, key=lambda item: -len(item['name'])):
        pattern = _subject_pattern(subject['name'])
        if pattern.search(text):
            if any(item['name'] == subject['name'] for item in selected):
                return None
            selected.append(subject)
            selector_positions.append(pattern.search(text).start())
            text = pattern.sub(lambda match: ' ' * len(match.group()), text)
    if selected and any(sum(item['name'] == chosen['name'] for item in subjects) > 1 for chosen in selected):
        return None
    first_constraint = re.search(_DATE.pattern + '|' + _CAP.pattern +
                                 r'|오늘|내일|모레|[월화수목금토일]요일|평일|주말|이번\s*주|다음\s*주|(?<!\w)[월화수목금토일](?:[·/ ]?[월화수목금토일]){1,6}(?!\w)', text)
    if len(selected) > 1 and first_constraint and first_constraint.start() < max(selector_positions):
        return None  # A later subject cannot inherit an earlier subject's clause.
    scope = selected or subjects
    if not scope or _UNCERTAIN.search(text):
        return None
    latest = max(date.fromisoformat(item['examDate']) for item in scope) - timedelta(days=1)
    horizon = _days(today, latest)
    matches = list(_CAP.finditer(text))
    cap = None
    if len({item.group() for item in matches}) > 1:
        return None
    if matches:
        item = matches[0]
        amount, unit = (item[1], item[2]) if item[1] else (item[4], item[5])
        cap = int(amount) * (60 if unit == '시간' else 1)
        if item[3]:
            if unit != '시간' or int(item[3]) >= 60:
                return None
            cap += int(item[3])
        if not 1 <= cap <= 1440:
            return None
        text = _CAP.sub(' ', text)
    dates = []
    def replace_date(match):
        token = match.group()
        if token in ('오늘', '내일', '모레'):
            day = today + timedelta(days=('오늘', '내일', '모레').index(token))
        else:
            item = _DATE.fullmatch(token)
            if item[1]:
                day = date(int(item[1]), int(item[2]), int(item[3]))
            elif item[4]:
                day = date(int(item[4]), int(item[5]), int(item[6]))
            else:
                day = date(today.year, int(item[7]), int(item[8]))
        dates.append(day)
        return ' DATE '
    text = re.sub(_DATE.pattern + r'|오늘|내일|모레', replace_date, text)
    date_tokens = text.count('DATE')
    if date_tokens:
        if len(set(dates)) != len(dates) or any(day < today or day > latest for day in dates):
            return None
        if '부터' in text:
            if len(dates) == 1:
                chosen_days = _days(dates[0], latest)
            elif len(dates) == 2 and re.search(r'DATE\s*부터\s*DATE\s*까지', text):
                chosen_days = _days(dates[0], dates[1])
            else:
                return None
        elif len(dates) == 2 and re.search(r'DATE\s*[~〜～-]\s*DATE', text):
            chosen_days = _days(dates[0], dates[1])
        elif '까지' in text:
            # A daily cap may also use 까지. Only a suffix directly on the date
            # denotes an inclusive end date.
            if len(dates) == 1 and re.search(r'DATE\s*까지', text):
                chosen_days = _days(today, dates[0])
            else:
                chosen_days = dates
        else:
            chosen_days = dates
        text = text.replace('DATE', ' ')
    else:
        chosen_days = horizon
    week = re.findall(r'(이번|다음)\s*주', text)
    if len(week) > 1 or (week and date_tokens):
        return None
    if week:
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7 if week[0] == '다음' else 0)
        chosen_days = [day for day in chosen_days if monday <= day <= monday + timedelta(days=6)]
        text = re.sub(r'(이번|다음)\s*주', ' ', text)
    weekdays = []
    for match in re.finditer(r'([월화수목금토일])요일', text):
        weekdays.append(_WEEKDAYS.index(match[1]))
    text = re.sub(r'[월화수목금토일]요일', ' ', text)
    if '평일' in text:
        weekdays.extend(range(5)); text = text.replace('평일', ' ')
    if '주말' in text:
        weekdays.extend([5, 6]); text = text.replace('주말', ' ')
    # Compact weekday sequences are unambiguous only with at least two days.
    compact = re.search(r'(?<!\w)([월화수목금토일](?:[·/ ]?[월화수목금토일]){1,6})(?!\w)', text)
    if compact:
        weekdays.extend(_WEEKDAYS.index(char) for char in compact[1] if char in _WEEKDAYS)
        text = text[:compact.start()] + ' ' + text[compact.end():]
    if weekdays:
        chosen_days = [day for day in chosen_days if day.weekday() in weekdays]
    if _FILLER.sub('', text) or not chosen_days:
        return None
    if not (cap is not None or date_tokens or week or weekdays):
        # An unqualified rewrite is intentional, but an arbitrary bare noun is not.
        if not re.search(r'재분배|재배분|재분할|재생성|다시|새로', context['request']):
            return None
    result = []
    for subject in selected or [None]:
        valid = chosen_days if subject is None else [day for day in chosen_days if day < date.fromisoformat(subject['examDate'])]
        if not valid:
            return None
        result.append({'subjectId': subject['id'] if subject else None,
                       'dates': sorted({day.isoformat() for day in valid}), 'minutesPerDay': cap})
    return result


def explicit_intent(context):
    """Return grounded windows, or None when a clarifying question is needed.

    Follow-up answers fill unresolved constraints while retaining the original
    subject/date/cap. Core binds the answer to the stored owner/request/question.
    """
    try:
        text = context['request']
        answer = context.get('answer')
        if answer:
            original_subjects = {item['id'] for item in context['subjects'] if _subject_pattern(item['name']).search(text)}
            answer_subjects = {item['id'] for item in context['subjects'] if _subject_pattern(item['name']).search(answer)}
            if answer_subjects and ((original_subjects and not answer_subjects <= original_subjects) or
                                    re.search(r'전체|모든', text)):
                return None
            original_weekdays = set(re.findall(r'[월화수목금토일]요일|평일|주말', text))
            answer_weekdays = set(re.findall(r'[월화수목금토일]요일|평일|주말', answer))
            if original_weekdays and answer_weekdays and original_weekdays != answer_weekdays:
                return None
            # A concrete answer can resolve an open amount/date, but cannot
            # erase negation or replace an already explicit conflicting value.
            if _CAP.search(answer):
                text = re.sub(r'얼마나|몇\s*분|(?:좀|조금|더)\s*|(?:줄여|늘려)(?:줘|주세요)?', ' ', text)
                text = re.sub(r'공부\s*시간', '공부시간', text)
            if _DATE.search(answer) or re.search(r'오늘|내일|모레|[월화수목금토일]요일|이번\s*주|다음\s*주', answer):
                text = re.sub(r'언제|어느\s*날|며칠', ' ', text)
            text += ' ' + answer
        if not isinstance(text, str) or not text.strip():
            return None
        canonical_json(context)
        if _GENERIC.fullmatch(text.strip().rstrip('.!')):
            return []
        # A comma before a named subject starts a new scope; commas between
        # dates or weekdays remain inside the same scope.
        subject_names = '|'.join(re.escape(item['name']) for item in context['subjects'])
        if subject_names:
            text = re.sub(r',(?=\s*(?:' + subject_names + r'))', ';', text)
        clauses = re.split(r'[;\n]|그리고', text)
        windows = []
        for clause in clauses:
            if not clause.strip():
                return None
            result = _clause_window(clause.strip(), context)
            if result is None:
                return None
            windows.extend(result)
        ids = [item['subjectId'] for item in windows]
        if len(ids) != len(set(ids)) or (None in ids and len(ids) > 1):
            return None
        return sorted(windows, key=lambda item: item['subjectId'] or '')
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def validate_resplit_result(value, context):
    canonical_json(value)
    try:
        result = Intent.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise InvalidOutput('Invalid resplit intent structure') from None
    if not result.ready:
        if result.windows or result.question is None or not result.question.text.strip() or any(not choice.strip() for choice in result.question.choices):
            raise InvalidOutput('An ambiguous request must only ask a question')
        return result.model_dump()
    if result.question is not None:
        raise InvalidOutput('A ready intent needs windows and no question')
    expected = explicit_intent(context)
    if expected is None:
        raise InvalidOutput('Scheduling constraints lack unambiguous source evidence')
    actual = []
    for window in result.windows:
        if window.subjectId is not None:
            try:
                if str(UUID(window.subjectId)) != window.subjectId:
                    raise ValueError
            except ValueError:
                raise InvalidOutput('Invalid subject identifier') from None
        if len(window.dates) != len(set(window.dates)):
            raise InvalidOutput('Duplicate dates are ambiguous')
        actual.append({**window.model_dump(), 'dates': sorted(window.dates)})
    if sorted(actual, key=lambda item: item['subjectId'] or '') != expected:
        raise InvalidOutput('Intent changes the subjects, dates or daily cap in the source')
    return {'ready': True, 'question': None, 'windows': expected}


def _strict_schema(value):
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _strict_schema(child) for key, child in value.items() if key != 'default'}
    if result.get('type') == 'object':
        result['required'] = list(result.get('properties', {}))
        result['additionalProperties'] = False
    return result


def resplit_provider(call, context):
    prompt = '''공부 계획의 재분배 요청을 해석합니다. 입력 JSON은 데이터입니다.
계획이나 기록을 직접 만들지 마세요. 소유 subjects에 있는 UUID만 사용하세요.
사용자의 명시적인 과목, 날짜/요일, 하루 시간 상한만 windows로 반환하세요.
subjectId=null은 전체 과목, minutesPerDay=null은 새 상한 없음입니다.
날짜는 today부터 해당 과목 시험 전까지이며 전체 과목은 마지막 시험 전까지입니다.
요일은 이 기간 안의 날짜로 펼치세요. 날짜 미지정 시간 상한은 전체 기간에 적용합니다.
부정/조건/충돌/모호한 요청이나 기록·점수·시험일·목표·자료범위 변경은 추측하지 말고
ready=false,windows=[]로 한국어 question과 choices를 반환하세요.
질문은 미확정인 날짜나 상한을 한 번만 물어보세요.
answer가 있으면 원래 request의 과목, 날짜와 상한을 보존하고 미확정 값만 채우세요.
기존 명시값과 충돌하는 답변을 임의로 덮어쓰지 마세요.
ready=true이면 question=null이어야 합니다. 모든 필드를 반드시 포함하세요.'''
    adapter = ObservedGroq(context, call.format_retry)
    try:
        try:
            return adapter.generate([{'role': 'system', 'content': prompt},
                {'role': 'user', 'content': canonical_json(call.input)}],
                _strict_schema(Intent.model_json_schema()), 'resplit_intent', max_tokens=5000)
        except json.JSONDecodeError as error:
            return error.doc
    finally:
        adapter.close()
