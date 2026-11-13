# 오류 코드 사전

기존 FE 분기 코드 목록이다. 최종 HTTP 상태는 common.md의 예외표가 우선한다. 추가 코드: PREVIEW_STALE, PREVIEW_REQUIRED, IDEMPOTENCY_CONFLICT, REQUEST_IN_PROGRESS, PLAN_INFEASIBLE, PLAN_CHANGE_REQUIRED, ONBOARDING_COMPLETED, REVIEW_NOT_DUE(409); FILE_TOO_LARGE(413), INVALID_INPUT(422), RATE_LIMITED(429), SERVER_ERROR(500), QUIZ_FINALIZED/SESSION_USED/SESSION_RUNNING(409), QUIZ_LOCKED/DEMO_DISABLED(403), NO_SESSION/UNKNOWN_SESSION(401). 이외 미등록 code를 임의 추가하지 않는다.



화면 흐름을 나누는 코드 — **서버가 이 코드 그대로 보내야 함** (HTTP 상태는 목업 값. 프론트는 상태가 아니라 code로 나눠요)

| code        | HTTP                         | 나오는 곳                                                            | 프론트 동작                            |
| ----------- | ---------------------------- | -------------------------------------------------------------------- | -------------------------------------- |
| `AI_FAILED` | 503                          | AI가 만드는 응답 6곳 (AI 상태 표)                                    | 실패 문구 + [다시 시도] · 저장된 결과  |
| `NO_PLAN`   | 404 (today) · 400 (next-day) | `GET /api/today` (계획 없음) · `POST /api/demo/next-day`             | 홈: '아직 계획이 없어요' + 계획 만들기 |
| `NO_QUIZ`   | 404                          | `GET /api/quiz/:subjectId` · `POST /api/quiz/:subjectId/submit`      | '이 과목 문항은 아직 준비 중이에요'    |
| `NO_RESULT` | 404 (result) · 400 (more)    | `GET /api/quiz/:subjectId/result` · `POST /api/quiz/:subjectId/more` | 결과 화면: '아직 푼 퀴즈가 없어요'     |

서버 코드가 아님

| code           | 설명                                                                                                |
| -------------- | --------------------------------------------------------------------------------------------------- |
| `ECONNABORTED` | 브라우저(axios)의 **30초 시간 초과**. 서버가 보내지 않아요. 프론트는 `AI_FAILED`와 같은 실패로 봐요 |

나머지 — 화면에 `error.message`가 그대로 보이는지 (코드 확인)

- **보임** = 화면이 알림(toast)이나 안내 문구에 `message`를 그대로 띄워요 → **`message`는 사용자에게 보여줄 한국어 문구로**
- **안 보임** = 화면이 '불러오지 못했어요' 같은 고정 문구를 써요 → `message`는 화면에 안 나와요 (한국어 권장, 필수 아님)

| code                  | 나오는 경로 (목업)                                                                                               | message |
| --------------------- | ---------------------------------------------------------------------------------------------------------------- | ------- |
| `ALREADY`             | `POST /api/quiz/:subjectId/more` · `POST /api/review/relearn`                                                    | 보임    |
| `BAD_DATE`            | `PATCH /api/subjects/:id/preview`                                                                                | 보임    |
| `BAD_KEY`             | `POST /api/plan/replan`                                                                                          | 보임    |
| `BAD_MINUTES`         | `POST /api/plan/additions/preview`                                                                               | 보임    |
| `BAD_OPTION`          | `POST /api/plan/empty-time`                                                                                      | 보임    |
| `BAD_PAGE`            | `PATCH /api/tasks/:id/record`                                                                                    | 보임    |
| `BAD_RANGE`           | `POST /api/plan/additions/preview`                                                                               | 보임    |
| `BAD_REQUEST`         | `POST /api/materials`                                                                                            | 보임    |
| `BAD_SEED`            | `POST /api/demo/seed`                                                                                            | 보임    |
| `BAD_TARGET`          | `PATCH /api/subjects/:id/preview` (보임) · `POST /api/demo/ai` (/dev, 안 보임)                                   | 보임    |
| `CONFIDENCE_REQUIRED` | `POST /api/review/notes/:id/retry` (보임) · `POST /api/quiz/:subjectId/submit` (채점 대기 화면, 안 보임)         | 보임    |
| `DEMO_END`            | `POST /api/demo/next-day`                                                                                        | 보임    |
| `EMPTY`               | `POST /api/onboarding/message` · `POST /api/materials/toc` · `POST /api/tutor` (튜터는 AI 실패 화면)             | 보임    |
| `ENDED`               | `POST /api/study/sessions/:id/resume`                                                                            | 보임    |
| `EVENT_NOT_FOUND`     | `PATCH /api/onboarding/events/:id`                                                                               | 보임    |
| `JOB_NOT_FOUND`       | `GET /api/materials/jobs/:jobId`                                                                                 | 안 보임 |
| `MATERIAL_NOT_FOUND`  | `PATCH /api/materials/:id/units`                                                                                 | 보임    |
| `NOT_ACTIVE`          | `POST /api/study/sessions/:id/pause`                                                                             | 보임    |
| `NOT_PDF`             | `POST /api/materials`                                                                                            | 보임    |
| `NOT_READY`           | `POST /api/plan/today/preview`                                                                                   | 보임    |
| `NOT_RECORDED`        | `PATCH /api/tasks/:id/record`                                                                                    | 보임    |
| `NO_CHANGE`           | `PATCH /api/subjects/:id/preview`                                                                                | 보임    |
| `NO_DAY`              | `PATCH /api/subjects/:id/preview`                                                                                | 보임    |
| `NO_DAYS`             | `POST /api/plan/additions/preview`                                                                               | 보임    |
| `NO_EMPTY_TIME`       | `POST /api/plan/empty-time`                                                                                      | 보임    |
| `NO_FILE`             | `GET /api/materials/:id/file` (PDF 미리보기 안내 문구)                                                           | 보임    |
| `NO_MATERIAL`         | `POST /api/plan/additions/preview`                                                                               | 보임    |
| `NO_PACE`             | `POST /api/plan/pace`                                                                                            | 보임    |
| `NO_PREVIEW`          | `PATCH/DELETE /api/subjects/:id` · `POST /api/plan/today`                                                        | 보임    |
| `NO_QUESTION`         | `GET /api/review/notes/:id/question` (안 보임) · `POST /api/review/notes/:id/retry` (보임)                       | 보임    |
| `NO_QUESTIONS`        | `POST /api/quiz/:subjectId/more` · `POST /api/review/custom-quiz`                                                | 보임    |
| `NO_REPLAN`           | `GET /api/plan/replan/options` (AI 실패 화면, 안 보임) · `POST /api/plan/replan` (보임)                          | 보임    |
| `NO_RESPLIT`          | `GET /api/plan/resplit` (고정 문구 '비교할 새 안이 없어요', 안 보임) · `POST /apply` · `DELETE` (보임)           | 보임    |
| `NO_TASK`             | `POST /api/plan/today/preview`                                                                                   | 보임    |
| `NO_UNITS`            | `POST /api/review/custom-quiz`                                                                                   | 보임    |
| `OTHER_ACTIVE`        | `POST /api/study/sessions` (집중 모드 시작 오류 화면 설명)                                                       | 보임    |
| `PARSE_FAILED`        | `POST /api/schedules/voice`                                                                                      | 보임    |
| `SCHEDULE_NOT_FOUND`  | `DELETE /api/schedules/:id`                                                                                      | 보임    |
| `SESSION_NOT_FOUND`   | `GET /api/study/sessions/:id` (안 보임) · `POST …/:id/pause\|resume\|end` (보임)                                 | 보임    |
| `SUBJECT_NOT_FOUND`   | `GET /api/units` (안 보임) · `POST /api/review/custom-quiz` (보임)                                               | 보임    |
| `TASK_NOT_FOUND`      | `POST /api/records` · `PATCH /api/tasks/:id/record` · `POST /api/study/sessions` · `POST /api/records/interpret` | 보임    |
| `UNIT_NOT_FOUND`      | `POST /api/review/relearn`                                                                                       | 보임    |

- 한 코드가 '보임' 경로와 '안 보임' 경로에 모두 나오면 '보임'으로 적었어요 (한국어 문구로 보내 주세요).
- `/dev` 전용 `POST /api/demo/ai`의 오류는 화면에 안 나와요.

