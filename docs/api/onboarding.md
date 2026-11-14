# onboarding API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- subjects[].status는 시험/목표 미수집 pending, 추가 응답 필요 asking, 시험일·목표 확인 완료 confirmed. **와이어 S1:** done은 과목이 하나 이상이고 각 과목의 시험일·목표 학점·시험 총 목표·기타 정보4칸을 확인한 경우에만 true. 선택 정보가 없을 때의 명시적 없음 응답 표현은 요구사항에 정의되지 않아 확인 상태로 구분하며 null만으로 확인 완료를 추정하지 않는다.
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


## 신규 과목 온보딩 컨텍스트

기존 초기 온보딩 경로를 유지한다. 승인된 계획에 과목을 추가할 때 `GET /api/onboarding?subjectId=…`, `POST /api/onboarding/message {subjectId,text}`로 draft 과목 컨텍스트를 지정한다. 해당 draft의 messages/chips/subjects/done만 반환하고 기존 활성 과목의 목표·시험·일정은 변경하지 않는다. 다른 사용자 또는 활성 과목을 컨텍스트로 전달하면404 SUBJECT_NOT_FOUND/409 ONBOARDING_COMPLETED. 초기 온보딩에서는 subjectId를 생략한다.

subjects[].confirmedFields에 `{examDate:boolean,goal:boolean,goalTotal:boolean,goalEtc:boolean}`를 필수로 반환한다. 기타 정보에 명시적으로 '없음'이라고 확인한 경우 값은 null, 확인 상태는 true일 수 있다. done은 네 확인값과 유효한 시험일·목표를 기준으로 서버가 계산한다. FE는 null 여부로 done을 재계산하지 않는다. 자연어로 날짜/목표가 불명확하면 추가 질문만 하고 confirmed로 바꾸지 않는다.

## 대화 해석과 재시도 (O-03)

- `POST /api/onboarding/message`는 bounded AI gateway에서 소유 draft 과목 문맥만 해석한다. DB 트랜잭션 밖에서 공급자를 호출하며 SDK 재시도는 0, 공통 요청 예산과 실제 호출 사용량 기록을 적용한다. 실패/키 누락/시간초과를 확정된 목표로 저장하지 않는다.
- 구조화 출력의 isConfirmed=false는 질문만 저장한다. true일 때도 네 confirmedFields를 따로 검사하며 각 true에는 현재 사용자 발화에 실제로 존재하는 인용 근거가 필요하다. 수정한 값만 해당 확인 상태를 무효화하고, 다른 칸에 false가 왔다는 이유로 기존 확인을 지우지 않는다. 목표 학점 확인에는 유효한 수치 목표도 필요하다.
- 요청 전후에 현재 날짜·시간대·과목 상태/값·대화·일정·계획 기준을 비교한다. 해석 도중 변경은 `409 INPUT_STALE`이고 과목/대화 변경을 전혀 반영하지 않는다. 한 발화의 목표와 사용자/AI 메시지는 한 트랜잭션으로 저장한다.
- 선택 헤더 `Idempotency-Key`(공백 없는 ASCII 1~128자)를 보내면 같은 사용자/경로/키의 성공 응답을 보관하고 응답 손실 후 그대로 재생한다. 다른 body로 키를 재사용하면 `409 IDEMPOTENCY_CONFLICT`, 진행 중이면 `409 REQUEST_IN_PROGRESS`다. 실패한 요청은 같은 키로 다시 시도할 수 있으며 lease 세대가 다른 늦은 응답은 저장하지 않는다.
- 헤더를 생략한 요청은 새 발화다. 예를 들어 서로 다른 확인 질문에 두 번 답한 `없음`을 중복 제거하지 않는다. 클라이언트가 응답 손실을 구분해 재시도하려면 동일 키를 유지한다. 이 헤더는 기존 요청 body의 필수 필드를 늘리지 않는다.
