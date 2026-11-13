# study API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- GET today는 승인 계획 없으면 404 NO_PLAN. tasks는 오늘의 현재 planned/partial/completed/missed, excluded/carried_over 과거 배치는 제외한다. notice/paceAlert/replan은 없으면 null, 배열은 빈 배열.
- 기록 result는 done/partial/none. partial만 toPage 필수이며 현재 task 범위 내 정수; done은 전체 범위, none은 0쪽. from/to가 없는 총복습 태스크에는 partial 금지(400 BAD_PAGE).
- 같은 task의 기록 수정은 기존 기록/범위를 교체하고 모든 집계를 재계산한다. 별도 회독이나 책을 중복 생성하지 않는다. 과거 기록의 study_date는 보존한다. 범위 밖 toPage는 clamp하지 않고 거절한다.
- sessionId는 같은 사용자/태스크의 ended 또는 auto_stopped 세션만. 다른 기록에 사용됐으면 409 SESSION_USED. 동일 기록에 동일 세션을 재전송하면 허용한다. 기록에 minutes 입력은 받지 않는다.
- PATCH record는 기존 완료/부분 기록만 허용. 완료→부분 수정 시 회독/블록·진도를 재판정하되 이미 저장한 과거 마감 분모를 바꾸거나 새 의무를 자동 추가하지 않는다.
- interpret는 text를 페이지 후보로만 해석, 기록 저장 없음. 모호하거나 범위 밖이면 400 PARSE_FAILED.
- P1 dayClose는 오늘 모든 태스크가 기록되고 진행률>=70일 때 사용자/날짜당 한 번. 일반 기록은 toast 또는 event:null. 중복 제출로 박스를 재발급하지 않는다.
- 이월은 기존 배치를 carried_over로 보존하고 같은 block_id로 새 배치를 만든다. 미완료 페이지는 합집합으로 계산. 미완료량 >= 남은 계획일의 하루 평균 계획 페이지 × 0.5면 replan, 미만이면 carry. 미완료가 0이면 none, 남은 계획일이 0이면 배정 불가 안내.
- replan A는 가용 시간 내 추가 배정, B는 미학습·비강조 범위를 제외, C는 남은 범위를 압축 복습. 기록된 범위를 삭제하지 않는다. 불가능한 안은 enabled:false. 세 안을 항상 반환하되 모두 불가능해도 거짓 실행안을 만들지 않는다.
- GET replan/options는 {options,saved,previewId}. POST replan은 key A/B/C와 previewId. 적용 {event:{type:"box",reason:"recover",...}}. 선택한 disabled 안은 400 BAD_KEY.
- 속도 보정은 최근 3일의 유효 종료 측정 시간/실제 완료 페이지로 계산하며 autoStopped/excluded는 제외. 샘플이 없으면 paceAlert:null. apply:false는 제안만 해제, true는 승인 속도와 미래 예상 시간 갱신; 일정 초과 시 replan=true. P2가 P4보다 먼저 표시된다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/today` — 홈

| 응답                                            | 타입     | null 가능 | 설명                                           |
| ----------------------------------------------- | -------- | --------- | ---------------------------------------------- |
| `date`                                          | string   | 아니요    |                                                |
| `subjects[].id` · `name` · `color` · `examDate` | string   | 아니요    |                                                |
| `tasks[]`                                       | Task[]   | 아니요    | 아래 Task                                      |
| `progress.doneCount` · `totalCount`             | number   | 아니요    | 오늘 할 일 n/m                                 |
| `progress.donePages` · `totalPages`             | number   | 아니요    |                                                |
| `progress.rate`                                 | number   | 아니요    | 오늘 진행률 % (서버 계산)                      |
| `quiz.openRate`                                 | number   | 아니요    | 70                                             |
| `quiz.open`                                     | bool     | 아니요    | 오늘 70% 이상                                  |
| `quiz.remainPages`                              | number   | 아니요    | 70%까지 남은 페이지                            |
| `quiz.openSubjects[]`                           | array    | 아니요    | `{ subjectId, count }` 과목별로 먼저 열린 퀴즈 |
| `quiz.totalCount`                               | number   | 아니요    | 오늘 퀴즈 문항 수                              |
| `exams[].subjectId` · `dday`                    | –        | 아니요    |                                                |
| `exams[].readiness`                             | number   | 예        | 준비도 %                                       |
| `recentDays[].date`                             | string   | 아니요    | 최근 7일 (오늘 포함)                           |
| `recentDays[].blocks[].state`                   | string   | 아니요    | 블록 상태 (enum 사전)                          |
| `recentDays[].blocks[].taskId`                  | string   | 예        | 오늘 블록이면                                  |
| `notice`                                        | object   | 예        | 이월 · 분배안 적용 안내 (아래)                 |
| `tutorSuggestions`                              | string[] | 예 ([])   | 튜터 추천 질문 칩                              |
| `paceAlert`                                     | object   | 예        | 속도 보정 알림 (P4 절)                         |
| `replan`                                        | object   | 예        | 분배안 (기준을 넘었을 때만, P2)                |

Task

| 필드               | 타입   | null 가능 | 설명                                   |
| ------------------ | ------ | --------- | -------------------------------------- |
| `id` · `subjectId` | string | 아니요    |                                        |
| `unitNo`           | number | 아니요    |                                        |
| `title`            | string | 아니요    |                                        |
| `from` · `to`      | number | 아니요    | 쪽 범위 (`pages`는 프론트가 to−from+1) |
| `minutes`          | number | 아니요    | 예상 분                                |
| `reason`           | string | 예        | 배치 이유 (AI 문구)                    |
| `status`           | string | 아니요    | `todo` · `partial` · `done` · `none`   |
| `donePages`        | number | 아니요    |                                        |
| `doneTo`           | number | 예        | 부분 완료면 어디까지                   |
| `carried`          | bool   | 예        | 어제에서 이월 (S6-2)                   |
| `measuredMinutes`  | number | 예        | 집중 모드로 잰 분 (기록에 붙은 것)     |
| `pendingSessionId` | string | 예        | 재고 아직 기록 안 한 측정              |
| `pendingMinutes`   | number | 예        |                                        |

`notice`

| 필드                            | 타입   | null 가능 | 설명                                          |
| ------------------------------- | ------ | --------- | --------------------------------------------- |
| `type`                          | string | 아니요    | `carry` (이월) · `replanned` (분배안 적용 뒤) |
| `key`                           | string | 예        | 적용한 분배안 `A` · `B` · `C`                 |
| `items[].subjectId`             | string | 아니요    |                                               |
| `items[].from` · `to` · `pages` | number | 아니요    | 이월 범위                                     |
| `items[].dailyAvg`              | number | 아니요    | 그 과목 하루 평균 계획 페이지                 |
| `items[].ratio`                 | number | 아니요    | 이월 / 하루 평균 %                            |

`replan` (`GET /api/plan/replan/options` 응답도 같은 모양)

| 필드                                         | 타입   | null 가능    | 설명                                  |
| -------------------------------------------- | ------ | ------------ | ------------------------------------- |
| `kind`                                       | string | 예 (overdue) | `overdue` (밀림) · `pace` (속도 보정) |
| `saved`                                      | bool   | 예           | 저장된 결과                           |
| `title`                                      | string | 예           |                                       |
| `date`                                       | string | 아니요       |                                       |
| `totalPages`                                 | number | 아니요       | 밀린 페이지 합                        |
| `overdue[].subjectId` · `pages` · `dailyAvg` | –      | 아니요       |                                       |
| `keepNote`                                   | string | 아니요       | 그대로 두는 것 안내                   |
| `options[].key`                              | string | 아니요       | `A` · `B` · `C`                       |
| `options[].lines` · `effect` · `preview`     | –      | 아니요       | 위 P2 절                              |

#### `POST /api/records` — 기록 (S7)

| 요청        | 타입   | 필수      | 허용 값 · 설명              |
| ----------- | ------ | --------- | --------------------------- |
| `taskId`    | string | 예        |                             |
| `result`    | string | 예        | `done` · `partial` · `none` |
| `toPage`    | number | partial만 | 끝낸 페이지                 |
| `sessionId` | string | 선택      | 집중 모드에서 온 기록이면   |

응답 `event` (없으면 null — 아무 연출 없음)

| 필드                           | 타입   | null 가능 | 설명                                                                  |
| ------------------------------ | ------ | --------- | --------------------------------------------------------------------- |
| `type`                         | string | 아니요    | `box` (P1 박스) · `toast` (P1-1 알림)                                 |
| `reason`                       | string | 아니요    | box: `dayClose` · `unitDone` · `recover` / toast: `record` · `rate70` |
| `message` · `detail`           | string | 예        | toast 문구                                                            |
| `rate`                         | number | 예        | 오늘 진행률                                                           |
| `planKey`                      | string | 예        | 회복 박스: 적용한 분배안                                              |
| `recordedCount` · `totalCount` | number | 예        | 하루 마무리: 기록한 태스크 수                                         |
| `donePages` · `totalPages`     | number | 예        |                                                                       |
| `opened[]`                     | array  | 예 ([])   | 이번 기록으로 열린 퀴즈 `{ subjectId, count }`                        |

#### `PATCH /api/tasks/:id/record` — 페이지 고치기

| 요청     | 타입   | 필수 |
| -------- | ------ | ---- |
| `toPage` | number | 예   |

응답: `taskId` string · `status` string (Task status) · `doneTo` number · `rate` number

#### `POST /api/records/interpret` — 말로 기록 (AI)

| 요청     | 타입   | 필수 |
| -------- | ------ | ---- |
| `taskId` | string | 예   |
| `text`   | string | 예   |

응답: `from` number · `to` number · `label` string (예 `"BST 탐색·삽입·삭제"`)

#### `GET /api/plan/replan/options` (`?saved=1`) · `POST /api/plan/replan` — P2 분배안

- `options` 응답: 위 `replan`과 같은 모양 (AI 설명, 저장된 결과면 `saved: true`).
- `POST` 요청 `key` string 필수 (`A` · `B` · `C`). 응답 `event` (기록 응답과 같은 모양, `reason: "recover"`).

#### `POST /api/plan/pace` — P4 속도 보정

| 요청        | 타입   | 필수 | 설명                         |
| ----------- | ------ | ---- | ---------------------------- |
| `subjectId` | string | 예   |                              |
| `apply`     | bool   | 예   | [반영] true · [그대로] false |

응답은 화면이 읽지 않아요 (성공 뒤 홈을 다시 받음). 알림 내용은 `GET /api/today`의 `paceAlert`:

| `paceAlert` 필드                                           | 타입   | null 가능 | 설명                                                 |
| ---------------------------------------------------------- | ------ | --------- | ---------------------------------------------------- |
| `subjectId` · `subjectName`                                | string | 아니요    |                                                      |
| `ratio`                                                    | number | 아니요    | 예상보다 몇 배                                       |
| `perPage` · `expectedPerPage`                              | number | 아니요    | 1페이지당 실제 · 예상 분                             |
| `days` · `sessions`                                        | number | 아니요    | 최근 N일 · 집중 모드 기록 수                         |
| `rows[].taskId` · `date` · `title` · `from` · `to` · `now` | –      | 아니요    |                                                      |
| `rows[].after`                                             | number | 예        | null이면 그대로 (페이지 기준이 아닌 태스크)          |
| `overDays[]`                                               | array  | 아니요    | `{ date, total, free, over }` 하루 공부 시간 넘는 날 |
| `mockFilled`                                               | array  | 예 ([])   | mock 표시용 (실제 서버는 없어도 됨)                  |
