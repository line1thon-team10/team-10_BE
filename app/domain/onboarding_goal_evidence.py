"""Conservative source grounding for four independently confirmed goal groups.

Model confidence and an arbitrary verbatim quotation do not establish a fact.
Unrecognized or contradictory evidence must be clarified in another turn.
"""
from datetime import date, timedelta
import re

from app.domain.onboarding import GOAL_GROUPS

_LABELS = {
    'examDate': r'시험\s*(?:날짜|일|은|이)|시험일|응시일',
    'goal': r'(?<!총\s)목표|학점|점수',
    'goalTotal': r'총\s*목표|커트라인|컷\s*라인|합격\s*기준|통과\s*기준|등급\s*기준',
    'goalEtc': r'기타|그\s*외|추가\s*정보|반영\s*비율|배점|강조\s*(?:범위|내용)|오픈북|시험\s*정보',
}
_ABSENCE = re.compile(r'없(?:음|어|다|습니다|어요)?|없$|해당\s*없')
_AFFIRMATION = re.compile(r'^(?:네|예|응|맞아(?:요)?|맞습니다|확인(?:했어(?:요)?|합니다)?)[.!\s]*$')


def _last_question(context, group):
    messages = context.get('state', {}).get('messages', [])
    if not messages or messages[-1].get('role') != 'ai':
        return False
    question = messages[-1].get('text', '')
    mentioned = {key for key, pattern in _LABELS.items() if re.search(pattern, question)}
    # An answer to a question about several groups cannot confirm each one.
    if group == 'goalTotal':
        mentioned.discard('goal')  # "총 목표" also contains the generic noun.
    return mentioned == {group}


def _scope(quote, group):
    match = re.search(_LABELS[group], quote)
    if not match:
        return None
    tail = quote[match.end():]
    other = [_LABELS[key] for key in _LABELS if key != group and not (group == 'goalTotal' and key == 'goal')]
    boundary = re.search('|'.join(other), tail)
    return tail[:boundary.start()] if boundary else tail


def _dates(text, today):
    found = []
    for match in re.finditer(r'(?<!\d)(?:(\d{4})\s*(?:년|[-/.])\s*)?(\d{1,2})\s*(?:월|[-/.])\s*(\d{1,2})\s*일?', text):
        try:
            found.append(date(int(match[1] or today.year), int(match[2]), int(match[3])).isoformat())
        except ValueError:
            return {None}
    for token, offset in [('오늘', 0), ('내일', 1), ('모레', 2)]:
        if token in text:
            found.append((today + timedelta(days=offset)).isoformat())
    return set(found)


def grounded_goal_group(group, quote, *, values, fields, previous, context):
    """True only when the quoted user evidence supports the proposed group."""
    # A quoted token can occur verbatim inside a correction ("A+가 아니라 B+").
    # Require at least one non-negated occurrence in the complete user message.
    if group in ('examDate', 'goal'):
        occurrences = list(re.finditer(re.escape(quote), context['text']))
        if occurrences and all(re.match(r'\s*(?:은|는|이|가|을|를)?\s*(?:아니|말고|대신|취소)',
                                        context['text'][match.end():]) for match in occurrences):
            return False
    merged = {key: values[key] if key in fields else previous.get(key) for key in GOAL_GROUPS[group]}
    contextual = _last_question(context, group)
    if group == 'examDate':
        dates = _dates(quote, date.fromisoformat(context['today']))
        if dates:
            return dates == {merged['exam_date']}
        return contextual and bool(_AFFIRMATION.fullmatch(quote.strip())) and 'exam_date' not in fields
    if group == 'goal':
        grades = set(re.findall(r'(?<![A-Za-z])(?:[ABCDF](?:\+|-|0)?)(?![A-Za-z])|통과|합격', quote, re.I))
        grade = merged['target_grade']
        if not grades:
            return contextual and bool(_AFFIRMATION.fullmatch(quote.strip())) and not ({'target_grade', 'target_score'} & set(fields))
        normalized = {item.upper() if item.isascii() else item for item in grades}
        if normalized != {grade.upper() if grade and grade.isascii() else grade}:
            return False
        explicit_scores = {float(value) for value in re.findall(r'(\d+(?:\.\d+)?)\s*점', quote)}
        score = merged['target_score']
        mapping = {'A+': 90, 'B+': 80, '통과': 60, '합격': 60}
        if explicit_scores:
            return explicit_scores == {score}
        return score == mapping.get(grade) or ('target_score' not in fields and score == previous.get('target_score'))
    scope = _scope(quote, group)
    if scope is None:
        if not contextual:
            return False
        scope = quote
    nonempty = [value for value in merged.values() if value is not None and str(value).strip()]
    if not nonempty:
        return bool(_ABSENCE.search(scope)) and not re.search(r'아니|있|모르|몰라|미정', scope)
    # Optional free text is persisted as the user's words, without invented facts.
    return all(str(value).strip() in scope for value in nonempty)
