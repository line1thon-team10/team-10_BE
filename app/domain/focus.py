"""집중 세션 생성 당시 고정하는 문구."""
import hashlib

QUOTES = [  # 고정 문구 10개 (HM-10, 프론트 seed.js focusQuotes와 같음)
    "벼락치기 말고, 피뢰침.",
    "지금은 이 범위만 보면 돼요.",
    "오늘 한 페이지가 시험 날의 한 문제예요.",
    "다 못 해도 괜찮아요. 한 만큼 쌓여요.",
    "밀려도 무너지지 않아요. 다시 맞추면 돼요.",
    "천천히 읽은 페이지가 오래 남아요.",
    "오늘의 블록 하나, 지금 쌓는 중이에요.",
    "어려운 곳에서 멈춘 것도 공부예요.",
    "한 번 더 본 개념은 덜 흔들려요.",
    "시험은 한 번, 준비는 매일 조금씩.",
]


def quote_for(session_id):
    return QUOTES[int(hashlib.md5(str(session_id).encode()).hexdigest(), 16) % len(QUOTES)]
