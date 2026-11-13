# 경로별 계약 목록

FE mock handler의 모든 method/path를 포함한다. 각 문서의 필드 표는 data의 형식이다. 기본 성공 200, 공통 오류/인증은 common.md, 실패/부작용/상태 전이는 연결된 도메인 규칙을 적용한다. 실제 BE에 등록됐다는 뜻은 아니다.

| Method | Path | 계약 | 범위 |
|---|---|---|---|
| GET | `/api/books` | [books.md](books.md) | 구현 대상 |
| GET | `/api/calendar` | [calendar.md](calendar.md) | 구현 대상 |
| GET | `/api/demo/ai` | [session.md](session.md) | 선택 기능 |
| POST | `/api/demo/ai` | [session.md](session.md) | 선택 기능 |
| POST | `/api/demo/next-day` | [session.md](session.md) | 구현 대상 |
| POST | `/api/demo/reset` | [session.md](session.md) | 구현 대상 |
| POST | `/api/demo/seed` | [session.md](session.md) | 구현 대상 |
| GET | `/api/demo/state` | [session.md](session.md) | 구현 대상 |
| GET | `/api/materials` | [material.md](material.md) | 구현 대상 |
| POST | `/api/materials` | [material.md](material.md) | 구현 대상 |
| GET | `/api/materials/:id/file` | [material.md](material.md) | 구현 대상 |
| PATCH | `/api/materials/:id/units` | [material.md](material.md) | 구현 대상 |
| GET | `/api/materials/jobs/:jobId` | [material.md](material.md) | 구현 대상 |
| POST | `/api/materials/toc` | [material.md](material.md) | 구현 대상 |
| GET | `/api/monthly` | [calendar.md](calendar.md) | 구현 대상 |
| GET | `/api/monthly/report` | [calendar.md](calendar.md) | 구현 대상 |
| GET | `/api/onboarding` | [onboarding.md](onboarding.md) | 구현 대상 |
| GET | `/api/onboarding/calendar` | [onboarding.md](onboarding.md) | 구현 대상 |
| POST | `/api/onboarding/events` | [onboarding.md](onboarding.md) | 구현 대상 |
| DELETE | `/api/onboarding/events/:id` | [onboarding.md](onboarding.md) | 구현 대상 |
| PATCH | `/api/onboarding/events/:id` | [onboarding.md](onboarding.md) | 구현 대상 |
| POST | `/api/onboarding/events/voice` | [onboarding.md](onboarding.md) | 구현 대상 |
| POST | `/api/onboarding/message` | [onboarding.md](onboarding.md) | 구현 대상 |
| POST | `/api/plan/additions` | [manage.md](manage.md) | 구현 대상 |
| POST | `/api/plan/additions/preview` | [manage.md](manage.md) | 구현 대상 |
| POST | `/api/plan/approve` | [plan.md](plan.md) | 구현 대상 |
| GET | `/api/plan/draft` | [plan.md](plan.md) | 구현 대상 |
| POST | `/api/plan/empty-time` | [calendar.md](calendar.md) | 구현 대상 |
| POST | `/api/plan/pace` | [study.md](study.md) | 구현 대상 |
| POST | `/api/plan/rebuild` | [manage.md](manage.md) | 구현 대상 |
| POST | `/api/plan/replan` | [study.md](study.md) | 구현 대상 |
| GET | `/api/plan/replan/options` | [study.md](study.md) | 구현 대상 |
| DELETE | `/api/plan/resplit` | [plan.md](plan.md) | 구현 대상 |
| GET | `/api/plan/resplit` | [plan.md](plan.md) | 구현 대상 |
| POST | `/api/plan/resplit` | [plan.md](plan.md) | 구현 대상 |
| POST | `/api/plan/resplit/apply` | [plan.md](plan.md) | 구현 대상 |
| POST | `/api/plan/today` | [manage.md](manage.md) | 구현 대상 |
| POST | `/api/plan/today/preview` | [manage.md](manage.md) | 구현 대상 |
| PUT | `/api/quiz/:quizId/answers/:questionId` | [quiz.md](quiz.md) | 원문/BE 기반 추가 |
| GET | `/api/quiz/:subjectId` | [quiz.md](quiz.md) | 구현 대상 |
| POST | `/api/quiz/:subjectId/more` | [quiz.md](quiz.md) | 구현 대상 |
| GET | `/api/quiz/:subjectId/result` | [quiz.md](quiz.md) | 구현 대상 |
| POST | `/api/quiz/:subjectId/submit` | [quiz.md](quiz.md) | 구현 대상 |
| GET | `/api/quiz/today` | [quiz.md](quiz.md) | 구현 대상 |
| POST | `/api/records` | [study.md](study.md) | 구현 대상 |
| POST | `/api/records/interpret` | [study.md](study.md) | 구현 대상 |
| POST | `/api/review/custom-quiz` | [review.md](review.md) | 구현 대상 |
| GET | `/api/review/notes` | [review.md](review.md) | 구현 대상 |
| GET | `/api/review/notes/:id/question` | [review.md](review.md) | 구현 대상 |
| POST | `/api/review/notes/:id/retry` | [review.md](review.md) | 구현 대상 |
| POST | `/api/review/relearn` | [review.md](review.md) | 구현 대상 |
| DELETE | `/api/schedules/:id` | [calendar.md](calendar.md) | 구현 대상 |
| PATCH | `/api/schedules/:id` | [calendar.md](calendar.md) | 원문/BE 기반 추가 |
| DELETE | `/api/schedules/:id/preview` | [calendar.md](calendar.md) | 원문/BE 기반 추가 |
| PATCH | `/api/schedules/:id/preview` | [calendar.md](calendar.md) | 원문/BE 기반 추가 |
| POST | `/api/schedules/voice` | [calendar.md](calendar.md) | 구현 대상 |
| POST | `/api/schedules/voice/apply` | [calendar.md](calendar.md) | 구현 대상 |
| POST | `/api/session` | [session.md](session.md) | 구현 대상 |
| POST | `/api/study/sessions` | [focus.md](focus.md) | 구현 대상 |
| GET | `/api/study/sessions/:id` | [focus.md](focus.md) | 구현 대상 |
| POST | `/api/study/sessions/:id/end` | [focus.md](focus.md) | 구현 대상 |
| POST | `/api/study/sessions/:id/pause` | [focus.md](focus.md) | 구현 대상 |
| POST | `/api/study/sessions/:id/resume` | [focus.md](focus.md) | 구현 대상 |
| GET | `/api/study/sessions/active` | [focus.md](focus.md) | 구현 대상 |
| GET | `/api/subjects` | [manage.md](manage.md) | 구현 대상 |
| DELETE | `/api/subjects/:id` | [manage.md](manage.md) | 구현 대상 |
| PATCH | `/api/subjects/:id` | [manage.md](manage.md) | 구현 대상 |
| DELETE | `/api/subjects/:id/preview` | [manage.md](manage.md) | 구현 대상 |
| PATCH | `/api/subjects/:id/preview` | [manage.md](manage.md) | 구현 대상 |
| PATCH | `/api/tasks/:id/record` | [study.md](study.md) | 구현 대상 |
| GET | `/api/today` | [study.md](study.md) | 구현 대상 |
| POST | `/api/tutor` | [tutor.md](tutor.md) | 구현 대상 |
| GET | `/api/tutor/backup` | [tutor.md](tutor.md) | 구현 대상 |
| GET | `/api/units` | [material.md](material.md) | 구현 대상 |
| PUT | `/api/units` | [material.md](material.md) | 구현 대상 |
