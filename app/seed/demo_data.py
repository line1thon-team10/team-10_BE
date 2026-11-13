"""11/12 합의 AI 데모 패키지. 날짜·목표·완료 상태는 시연용 고정 값이다.

단원·개념·PDF 페이지는 고정 AI submodule의 ground_truth/demo에서 읽는다.
"""
from datetime import date

SEEDS = {
    "empty": "빈 계정 · 계획 만들기 (11/3)",
    "day9": "11/9 기본 · 집중 42분",
    "overdue": "밀림 시나리오 (11/9, 인공지능·운영체제 미완료)",
}
DEFAULT_SEED = "day9"
SEED_DATES = {"empty": date(2026, 11, 3), "day9": date(2026, 11, 9), "overdue": date(2026, 11, 9)}
PLAN_START = date(2026, 11, 3)
MIN_PER_PAGE = 2.5
PACKAGE_VERSION = "demo-package-2026-11-14-v2"
SUBJECTS = [
    {"code": "AI", "name": "인공지능", "asset": "artificial_intelligence", "color": "#4F46E5",
     "exam_date": date(2026, 11, 12), "target_grade": "A+", "target_score": 90},
    {"code": "DB", "name": "데이터베이스", "asset": "database", "color": "#2563EB",
     "exam_date": date(2026, 11, 16), "target_grade": "B+", "target_score": 80},
    {"code": "OS", "name": "운영체제", "asset": "operating_system", "color": "#7C3AED",
     "exam_date": date(2026, 11, 18), "target_grade": "통과", "target_score": 60},
]
TYPE_MAP = {"객관식": "choice", "OX": "ox", "단답형": "short", "서술형": "essay"}
DIFF_MAP = {"하": "low", "중": "mid", "상": "high"}
CONF_MAP = {"확실": "sure", "애매": "unsure"}
EVENTS = [
    ("class", "수업", "MO,WE", "10:00", "12:00"),
    ("class", "수업", "TU,TH", "13:00", "15:00"),
    ("job", "카페 알바", "MO,WE,FR", "18:00", "22:00"),
    ("meet", "친구 약속", "SA", "18:00", "21:00"),
]
EVENT_UNTIL = date(2026, 11, 18)


def D(day):
    return date(2026, 11, day)


# 날짜, 과목, 단원, 종류, 범위, 분, 이유. 9p × 2.5분은 5분 단위로 반올림한 25분.
PLAN = [
    (D(3), "AI", 1, "study", [(1, 3, 11)], 25, "머신러닝 기초 먼저 학습"),
    (D(4), "DB", 1, "study", [(1, 3, 11)], 25, "관계형 데이터베이스 기초"),
    (D(5), "OS", 1, "study", [(1, 3, 11)], 25, "프로세스와 스레드 기초"),
    (D(9), "AI", 2, "study", [(2, 12, 20)], 25, "시험 D-3, 지도학습 알고리즘"),
    (D(9), "DB", 2, "study", [(2, 12, 20)], 25, "SQL 조회와 조작"),
    (D(9), "OS", 2, "study", [(2, 12, 20)], 25, "CPU 스케줄링"),
    (D(10), "AI", 3, "study", [(3, 21, 29)], 25, "시험 D-2, 모델 평가와 활용"),
    (D(11), "AI", None, "review", [], 25, "시험 전날 총복습"),
    (D(12), "DB", 3, "study", [(3, 21, 29)], 25, "정규화와 트랜잭션"),
    (D(15), "DB", None, "review", [], 25, "시험 전날 총복습"),
    (D(16), "OS", 3, "study", [(3, 21, 29)], 25, "교착상태와 기아상태"),
    (D(17), "OS", None, "review", [], 25, "시험 전날 총복습"),
]
TODAY_UNITS = {"AI": ["AI2"], "DB": ["DB2"], "OS": ["OS2"]}
DONE_ON_DAY9 = {"day9": {1, 2}, "overdue": {1}}
