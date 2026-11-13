# 피뢰침 백엔드 (team-10_BE)

FastAPI 서버 하나에 현민님 AI 코드(team-10_AI)를 함께 불러 쓰는 구조예요.
프론트(team-10_FE)는 이 서버의 `/api/...`를 불러요.

```
프론트 (Vercel) ──HTTP──▶ 백엔드 FastAPI ──함수 호출──▶ AI submodule (vendor/team-10_AI/services)
                              │
                              └── DB (로컬 SQLite / 배포 PostgreSQL)
```

## 기준 문서

| 무엇 | 어디 |
| --- | --- |
| DB 구조 | `schema/erd.dbml` (효원님 ERD) → `app/db/schema.py`로 자동 생성 |
| API 구현 기준 명세 | [API 명세 1.0](docs/api/README.md) — FE 기준 계약·상태 전이·오류·수용 기준 (팀 합의 전 제안) |
| 구버전 참고 명세 | `schema/openapi-0.7.0.json` — 현재 FE 계약과 경로/응답이 달라 직접 구현 기준으로 사용하지 않음 |
| 기능·규칙 | 피뢰침 요구사항 정의서 v2 |

## 로컬 실행

RPI 배포 구성·도메인·CI/CD·복구 절차는 [RPI 배포 안내](docs/rpi-deployment.md)를 참고하세요.

BE는 AI 저장소의 특정 커밋을 submodule로 포함해요. 별도 AI 형제 폴더는 필요하지 않아요. [AI 의존성·stacked 개발 안내](docs/ai-dependency.md)를 참고하세요.

```
피뢰침/
  team-10_BE/   ← 여기
    vendor/team-10_AI/  ← BE에 고정된 AI 커밋
  team-10_FE/
```

먼저 submodule을 포함해 clone하거나 기존 checkout에서 초기화하세요. AI 저장소 읽기 권한이 필요합니다.

```bash
git clone --recurse-submodules https://github.com/line1thon-team10/team-10_BE.git
# 기존 checkout에서는 BE 루트에서 실행
git submodule update --init --recursive
```

1. 가상환경과 패키지 (Python 3.12 권장)
   ```bash
   py -3.12 -m venv .venv
   .venv/Scripts/python -m pip install -r requirements.txt
   ```
2. AI 패키지 (team-10_AI 원본 목록의 PyMuPDF 버전 충돌을 고친 목록)
   ```bash
   .venv/Scripts/python -m pip install -r requirements-ai.txt
   ```
3. `.env.example`을 `.env`로 복사하고 `GROQ_API_KEY`, `UPSTAGE_API_KEY` 채우기
4. DB 준비: 신규 DB는 아래 명령을 실행하세요. 기존 DB는 먼저 [DB 인계 절차](docs/db-migrations.md)를 따르세요. 서버 시작은 스키마를 자동 변경하지 않아요.
   ```bash
   .venv/Scripts/python -m alembic upgrade head
   ```
5. 서버 실행
   ```bash
   .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
   ```
6. 확인: http://localhost:8000/docs , http://localhost:8000/api/health?ai=true

프론트를 붙일 때는 team-10_FE의 `.env`에 `VITE_API_BASE_URL=http://localhost:8000`, `VITE_USE_MOCK=false`.

## 공통 약속 (프론트와 맞춘 것)

- 응답: 성공 `{ "success": true, "data": ... }`, 실패 `{ "success": false, "error": { "code", "message" } }`
- 익명 ID: `POST /api/session` → `{ anonymousId }`, 이후 모든 요청 헤더 `X-Anonymous-Id`
- '오늘'은 서버가 정해요 (데모 사용자는 `users.demo_date`). 프론트는 브라우저 시계를 쓰지 않아요
- 값 이름은 프론트 키 (등급 risk/shaky/stable, 문항 choice/ox/short/essay, 확신도 sure/unsure …)

## 지금 되는 것 (0.1.0)

| API | 내용 |
| --- | --- |
| `GET /api/health` | 서버·DB 상태, `?ai=true`면 AI 모듈 불러오기 확인 |
| `POST /api/session` | 익명 ID 발급 (데모 모드면 11/9 시드 자동 적재) |
| `GET /api/demo/state` | 데모 날짜, 첫 시험, 시드 이름, 계획 유무 |
| `POST /api/demo/seed` | `{ seed: empty \| day9 \| overdue }` 시드 전환 |
| `POST /api/demo/reset` | 기본 시드(day9)로 초기화 |
| `POST /api/demo/next-day` | [다음 날로]: 오늘 마감 → 자정 판정 → `judgment: carry \| replan \| none` |
| `POST /api/study/sessions` | 집중 모드 [시작] `{ taskId }` (같은 태스크면 이어서) |
| `GET /api/study/sessions/active` | 진행 중인 세션 (없으면 null) |
| `GET /api/study/sessions/:id` | 세션 상태·측정 시간·오늘 총·과목 누적 |
| `POST /api/study/sessions/:id/pause` · `/resume` · `/end` | 멈춤 · 다시 시작(또는 '아직 공부 중'에 계속) · 종료 |
| `GET /api/plan/replan/options` | 분배안 A 시간 늘리기 / B 범위 줄이기 / C 압축하기 (밀렸을 때만) |
| `POST /api/plan/replan` | `{ key: A \| B \| C }` 고른 안 적용 → 회복 박스 이벤트 |
| `GET /api/today` | 홈: 오늘 할 일·진행률·퀴즈 열림(70%)·D-day·준비도·최근 7일 블록 |
| `POST /api/records` | 기록 `{ taskId, result: done\|partial\|none, toPage, sessionId }` → 박스/알림 이벤트 |
| `PATCH /api/tasks/:id/record` | 기록한 뒤 끝낸 페이지 고치기 |
| `GET /api/books` | 책 쌓기: 과목·단원별 블록 상태, 등급·점수·회독, 준비도(진도율 × 숙달도) |
| `GET /api/quiz/today` | 과목별 오늘의 퀴즈 카드: 문항 수·열림(70%)·구성·결과 |
| `GET /api/quiz/:key` | 문항 (key = 과목 ID 또는 더 풀기 퀴즈 ID). 정답·해설은 안 보내요 |
| `POST /api/quiz/:key/submit` | 제출 → 채점(서술형은 AI, 실패 시 저장된 채점) → 단원 점수(calc.py 식)·취약 문항 → 결과 |
| `GET /api/quiz/:key/result` | 결과: 정답·해설·근거, 단원 점수 변화, 퀴즈 후 책 쌓기(P3), 오답 노트 추가 |
| `POST /api/quiz/:subjectId/more` | 더 풀기: 같은 단원 안 푼 5문항 → `{ quizId, count }` |

시드(`app/seed/`)에는 과목 3개·단원 13개·개념 39개·고정 일정·2주 계획(26개 태스크)·11/9 오후 5시까지 기록, 그리고 현민님 `seed_quiz.json`의 문항 77개·풀이 기록·취약 문항·11/9 오늘 퀴즈(7·13·5문항)가 들어가요.
태스크 수는 데이터 세트(45개)와 아직 달라요. 15~60분 분할 규칙은 계획 엔진(백엔드 A)에서 맞춰요.

## 폴더

```
app/
  main.py          서버 진입점 (CORS, 에러 형식, 라우터 등록)
  config.py        환경변수
  core/            공통 응답·익명 인증·오늘 날짜
  db/schema.py     자동 생성 테이블 정의 (직접 고치지 않기)
  db/session.py    DB 연결·revision 검사
  ai/engine.py     AI 모듈 불러오기
  routers/         API (프론트 endpoints.js 기준으로 추가)
  seed/            데모 시드 값과 적재
vendor/team-10_AI/ AI 소스 submodule (별도 저장소/커밋)
migrations/        Alembic revision·불변 초기 스키마
schema/            ERD·참고 명세 원본
scripts/gen_schema ERD → schema.py 생성기 (Node)
```

## ERD가 바뀌면

```bash
cd scripts/gen_schema && npm install && node gen_schema.js
```

모델 재생성만으로 DB는 변경되지 않아요. 새 revision을 작성·검토·테스트한 뒤 적용하세요. [마이그레이션 개발 절차](docs/db-migrations.md)를 참고하세요.

## 알려진 점

- 준비도는 calc.py와 같아요: 11/9 오후 5시 43·33·34%, 자료구조 p.88 기록 뒤 53%, 자료구조 퀴즈(3단원 83→73) 뒤 51%.
- 시연 퀴즈 확인: 자료구조 7문항 중 5개 정답(3번 서술형 70점, 5번 오답) → 3단원 83→73, 준비도 51%.
- 자정 판정 확인: 11/9 p.88까지 기록 → 다음 날로 → 남은 p.89–95(7p)가 11/10 첫 태스크로 이월. 밀림 시드 → 다음 날로 → 38p(자료구조 24 · 네트워크 14, 기준 9p) → 분배안 3개 → 적용.
- 분배안 B는 밀린 과목마다 아직 안 한 마지막 개념의 끝 6p를 빼요(강조 단원 제외). 데이터 세트 예시(운영체제 세그멘테이션)와는 고르는 범위가 달라요.
- 아직 없는 규칙: 시험 당일엔 그 과목을 넣지 않기, 하루 공부 가능 시간 검사, 분배안 '나중에'. 며칠 건너뛰면 밀린 분량이 시험 당일에도 들어갈 수 있어요.
- 집중 모드: 서버 시계로 순공부 구간을 쌓아 재요(멈춘 시간 제외, 새로고침해도 이어짐). 예상 시간의 2배가 지나면 `checkIn`, 10분 무응답이면 알림 시점에서 자동 종료하고 속도 보정에서 빼요. 기록할 때 `sessionId`를 붙이면 측정 시간이 남아요(세션당 기록 1번).
- 서술형 AI 채점은 `GROQ_API_KEY`가 있어야 해요. 없거나 실패하면 저장된 채점(시연 문항 70점)을 쓰고 결과에 `essayGrading: saved`로 표시해요.
- 더 풀기는 '이미 푼 문항은 안 나옴' 규칙이라, 시드의 3단원처럼 문항을 다 푼 단원은 `NO_QUESTIONS`예요. AI 문항 생성(quiz_bank)을 붙이면 해결돼요.
- 퀴즈 시드는 비공개 AI 저장소의 `seed_quiz.json`을 `AI_ENGINE_PATH`에서 바로 읽어요. 이 공개 저장소에는 복사하지 않아요. 배포 빌드에서도 submodule을 초기화해 해당 BE 커밋에 고정된 AI 소스를 포함해야 해요.

- 윈도우 스마트 앱 컨트롤 때문에 SQLAlchemy 2.1·psycopg의 확장 모듈이 막혀서 SQLAlchemy 2.0을 써요. 로컬은 SQLite라 psycopg가 필요 없어요.
- ERD 노트의 부분 유일 인덱스(`erd-constraints.sql`)는 아직 받지 못해서 반영 전이에요.
