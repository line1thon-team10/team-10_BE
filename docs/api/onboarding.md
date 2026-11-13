# onboarding API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- subjects[].status는 시험/목표 미수집 pending, 추가 응답 필요 asking, 시험일·목표 확인 완료 confirmed. done은 과목이 하나 이상이고 모두 confirmed일 때 true.
- 메시지는 사용자 소유 대화에 저장한다. 해석 실패 시 승인된 과목/일정은 변경하지 않는다. 불명확한 날짜/시간은 질문하고 추측해 확정하지 않는다.
- Event의 days(중복 없는 1..7, 비어 있지 않음) 또는 date 중 정확히 하나를 받는다. start < end, 자정을 넘는 일정은 두 일정으로 입력한다.
- create는 kind/title/start/end와 days 또는 date 필수. PATCH는 부분 수정 허용하되 병합 후 동일 검증. 이벤트 수정/삭제는 draft 온보딩에서만 허용하며 승인 후에는 409 ONBOARDING_COMPLETED; 승인 후 변경은 schedules 사용.
- voice는 text를 해석해 확실한 일정 하나만 저장한다. 모호하면 400 PARSE_FAILED, 일정 저장 없음. 응답 addedId는 생성한 ID, 나머지 요청은 null.
- calendar의 자유 시간은 해당 주 매일 08:00~23:00에서 고정 일정들의 합집합을 뺀 분량. 겹침을 중복 차감하지 않는다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/onboarding` · `POST /api/onboarding/message`

| 요청 (`message`) | 타입   | 필수 | 설명           |
| ---------------- | ------ | ---- | -------------- |
| `text`           | string | 예   | 사용자가 쓴 말 |

| 응답                   | 타입     | null 가능 | 설명                                  |
| ---------------------- | -------- | --------- | ------------------------------------- |
| `messages[].role`      | string   | 아니요    | `ai` · `user`                         |
| `messages[].text`      | string   | 아니요    |                                       |
| `chips`                | string[] | 예 ([])   | 추가 질문 답 칩                       |
| `subjects[].id`        | string   | 아니요    |                                       |
| `subjects[].name`      | string   | 아니요    |                                       |
| `subjects[].examDate`  | string   | 예        |                                       |
| `subjects[].goal`      | string   | 예        | 목표 학점 (null → 화면에 [목표 학점]) |
| `subjects[].goalTotal` | string   | 예        | 시험 총 목표 (커트라인 · 합격 기준)   |
| `subjects[].goalEtc`   | string   | 예        | 기타 정보 (반영 비율 · 강조 범위)     |
| `subjects[].target`    | number   | 예        | 내부 목표 점수 (4-6)                  |
| `subjects[].status`    | string   | 아니요    | `pending` · `asking` · `confirmed`    |
| `schedules[]`          | Event[]  | 아니요    | 아래 Event                            |
| `done`                 | bool     | 아니요    | 모든 과목 목표 확정 → 다음 단계 가능  |

Event (일정 한 개, S1 · S2 공통)

| 필드    | 타입     | null 가능 | 설명                               |
| ------- | -------- | --------- | ---------------------------------- |
| `id`    | string   | 아니요    |                                    |
| `kind`  | string   | 아니요    | `class` · `job` · `meet` · `other` |
| `title` | string   | 아니요    |                                    |
| `days`  | number[] | 예        | 있으면 매주 반복 (1=월…7=일)       |
| `date`  | string   | 예        | 있으면 하루짜리                    |
| `start` | string   | 아니요    | `HH:mm`                            |
| `end`   | string   | 아니요    | `HH:mm`                            |
| `added` | bool     | 예        | 방금 말로 추가한 일정              |

#### `GET /api/onboarding/calendar` · `POST /api/onboarding/events` · `PATCH/DELETE /api/onboarding/events/:id` · `POST /api/onboarding/events/voice`

| 요청 (`POST` · `PATCH`) | 타입     | 필수 | 설명                           |
| ----------------------- | -------- | ---- | ------------------------------ |
| `kind`                  | string   | 예   | Event와 같음                   |
| `title`                 | string   | 예   |                                |
| `days`                  | number[] | 선택 | 반복이면                       |
| `date`                  | string   | 선택 | 하루짜리면                     |
| `start` · `end`         | string   | 예   | `HH:mm`                        |
| `text` (`/voice`만)     | string   | 예   | 예 `"토요일 저녁엔 친구 약속"` |

| 응답 (다섯 경로 같음)  | 타입    | null 가능 | 설명                 |
| ---------------------- | ------- | --------- | -------------------- |
| `weekStart`            | string  | 아니요    | 그 주 월요일         |
| `today`                | string  | 아니요    | 데모 날짜            |
| `events[]`             | Event[] | 아니요    |                      |
| `exams[].subjectId`    | string  | 아니요    |                      |
| `exams[].name`         | string  | 아니요    |                      |
| `exams[].examDate`     | string  | 아니요    |                      |
| `summary.fixedMinutes` | number  | 아니요    | 이번 주 고정 일정 분 |
| `summary.freeMinutes`  | number  | 아니요    | 이번 주 빈 시간 분   |
| `addedId`              | string  | 예        | 방금 추가한 일정 id  |
