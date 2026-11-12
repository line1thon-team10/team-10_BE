# 피뢰침 백엔드 (team-10_BE)

FastAPI 서버 하나에 현민님 AI 코드(team-10_AI)를 함께 불러 쓰는 구조예요.
프론트(team-10_FE)는 이 서버의 `/api/...`를 불러요.

```
프론트 (Vercel) ──HTTP──▶ 백엔드 FastAPI ──함수 호출──▶ AI 모듈 (team-10_AI/services)
                              │
                              └── DB (로컬 SQLite / 배포 PostgreSQL)
```

## 기준 문서

| 무엇 | 어디 |
| --- | --- |
| DB 구조 | `schema/erd.dbml` (효원님 ERD) → `app/db/schema.py`로 자동 생성 |
| API 경로·응답 모양 | 프론트 `team-10_FE/src/api/endpoints.js`, `src/api/adapters/`, `docs/api-draft.md` |
| 참고 명세 | `schema/openapi-0.7.0.json` (효원님 OpenAPI, 대회 후 서비스 버전 기준) |
| 기능·규칙 | 피뢰침 요구사항 정의서 v2 |

## 로컬 실행

폴더 구조는 세 저장소를 나란히 두는 걸 기준으로 해요.

```
피뢰침/
  team-10_BE/   ← 여기
  team-10_AI/
  team-10_FE/
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
4. 서버 실행
   ```bash
   .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
   ```
5. 확인: http://localhost:8000/docs , http://localhost:8000/api/health?ai=true

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
  db/session.py    DB 연결
  ai/engine.py     AI 모듈 불러오기
  routers/         API (프론트 endpoints.js 기준으로 추가)
  seed/            데모 시드 값과 적재
schema/            ERD·참고 명세 원본
scripts/gen_schema ERD → schema.py 생성기 (Node)
```

## ERD가 바뀌면

```bash
cd scripts/gen_schema && npm install && node gen_schema.js
```

## 알려진 점

- 준비도는 calc.py와 같아요: 11/9 오후 5시 43·33·34%, 자료구조 p.88 기록 뒤 53%, 자료구조 퀴즈(3단원 83→73) 뒤 51%.
- 시연 퀴즈 확인: 자료구조 7문항 중 5개 정답(3번 서술형 70점, 5번 오답) → 3단원 83→73, 준비도 51%.
- 자정 판정 확인: 11/9 p.88까지 기록 → 다음 날로 → 남은 p.89–95(7p)가 11/10 첫 태스크로 이월. 밀림 시드 → 다음 날로 → 38p(자료구조 24 · 네트워크 14, 기준 9p) → 분배안 3개 → 적용.
- 분배안 B는 밀린 과목마다 아직 안 한 마지막 개념의 끝 6p를 빼요(강조 단원 제외). 데이터 세트 예시(운영체제 세그멘테이션)와는 고르는 범위가 달라요.
- 아직 없는 규칙: 시험 당일엔 그 과목을 넣지 않기, 하루 공부 가능 시간 검사, 분배안 '나중에'. 며칠 건너뛰면 밀린 분량이 시험 당일에도 들어갈 수 있어요.
- 서술형 AI 채점은 `GROQ_API_KEY`가 있어야 해요. 없거나 실패하면 저장된 채점(시연 문항 70점)을 쓰고 결과에 `essayGrading: saved`로 표시해요.
- 더 풀기는 '이미 푼 문항은 안 나옴' 규칙이라, 시드의 3단원처럼 문항을 다 푼 단원은 `NO_QUESTIONS`예요. AI 문항 생성(quiz_bank)을 붙이면 해결돼요.
- 퀴즈 시드는 비공개 AI 저장소의 `seed_quiz.json`을 `AI_ENGINE_PATH`에서 바로 읽어요. 이 공개 저장소에는 복사하지 않아요. 배포 서버에도 AI 코드가 같이 있어야 해요.

- 윈도우 스마트 앱 컨트롤 때문에 SQLAlchemy 2.1·psycopg의 확장 모듈이 막혀서 SQLAlchemy 2.0을 써요. 로컬은 SQLite라 psycopg가 필요 없어요.
- ERD 노트의 부분 유일 인덱스(`erd-constraints.sql`)는 아직 받지 못해서 반영 전이에요.
