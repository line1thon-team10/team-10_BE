# calendar API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- calendar from/to는 양끝 포함, from<=to, 최대42일. 월간 month는 실제 연월, days는 해당 월 달력의 월요일 시작~일요일 종료 28/35/42칸. 모든 날짜는 서버 timezone.
- fixed.id는 일정UUID@원래회차날짜, scheduleId는 일정 UUID. 일정 수정/삭제의 :id에는 회차 ID 사용. once는 그 회차 취소 예외, future는 해당 원래 날짜부터 반복 종료. 원본 이전 회차·학습 기록 보존.
- 삭제도 먼저 미리보기하고 previewId로 승인한다. 삭제 승인 후 공부 태스크를 즉시 삭제하지 않는다. 빈 시간이 있으면 {emptyTime,options,previewId}, 없으면 {emptyTime:null,options:[],previewId:null}. rest/pull/advance 모두 반환, 불가능한 선택은 enabled:false.
- empty-time 적용은 option+previewId. rest는 공부 추가 없음, pull은 기존 미래 태스크를 앞당김, advance는 미학습 다음 범위 배정. 날짜·가용 시간·시험일·기록 보존 규칙 준수. 성공 {applied:true}.
- voice는 일정/이동 후보만 저장하며 승인 전 일정/태스크 변경 금지. event는 {title,date,start,end,repeat,label}, moves[].pieces는 실제 분할 범위. apply에 previewId 필수, 성공 {applied:true}.
- study block은 ranges와 pages를 study.md 형식으로 포함한다. 단원은 unitId/materialId로 식별한다. id/subjectId/subjectName/color/title/date/start/end/reason/status가 string, from/to는 integer|null, minutes는 integer, supplement/moved는 bool. exams는 {subjectId,name,examDate,color}[], summary는 과목 ID별 {minutes:integer,count:integer}.
- monthly subjects는 {id,name,color}[], days[].labels는 string[]. **와이어 S15·S15-1:** plan/done과 achievement는 새 범위와 보충을 포함한 일별 task 완료/계획 페이지 합, rate는 분모0이면0. subjects[].pages/share는 새 범위 고유 페이지 기준이며 보충 제외. 두 분모를 혼용하지 않는다. future에 완료량을 예측하지 않는다. estimated는 실제 측정 시간이 없는 계획 기반 표시에만 사용.
- report는 숫자 집계이며 AI 회고를 생성하지 않는다. from은 해당 월1일, until은 min(today,월말), 미래월은 studyDays=0 등 빈 집계. grades[].before는 {grade:string,score:number} 또는 null, after는 {grade:string,score:number}. share는 과목 학습페이지/전체 학습페이지의 백분율, 0분모면0.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/calendar?from=&to=` — 주간 캘린더

| 응답                                                               | 타입         | null 가능 | 설명                                |
| ------------------------------------------------------------------ | ------------ | --------- | ----------------------------------- |
| `from` · `to` · `today`                                            | string       | 아니요    |                                     |
| `exams`                                                            | array        | 아니요    | 위 S11 절                           |
| `fixed[].id`                                                       | string       | 아니요    | `'일정id@날짜'`                     |
| `fixed[].scheduleId` · `kind` · `title` · `date` · `start` · `end` | string       | 아니요    |                                     |
| `fixed[].repeat`                                                   | bool         | 아니요    | 반복 일정 (삭제 범위를 물어봐요)    |
| `fixed[].added`                                                    | bool         | 예        |                                     |
| `study[]`                                                          | StudyBlock[] | 아니요    | 아래                                |
| `summary`                                                          | object       | 아니요    | 이번 주 과목별 `{ minutes, count }` |

StudyBlock: `id` · `subjectId` · `subjectName` · `color` · `title` · `from` · `to` (총복습처럼 없으면 null) · `date` · `start` · `end` · `minutes` · `reason` · `supplement` bool · `status` · `moved` bool

#### `DELETE /api/schedules/:id/preview?scope=once|future` → `DELETE /api/schedules/:id?previewId=…` → 빈 시간 안 · `POST /api/plan/empty-time`

| 요청                    | 타입   | 필수 | 허용 값                     |
| ----------------------- | ------ | ---- | --------------------------- |
| `scope` (쿼리)          | string | 예   | `once` · `future`           |
| `option` (`empty-time`) | string | 예   | `rest` · `pull` · `advance` |

빈 시간 안 응답 (`DELETE /api/schedules/:id` · `DELETE /api/subjects/:id`)

| 필드                       | 타입   | null 가능 | 설명                          |
| -------------------------- | ------ | --------- | ----------------------------- |
| `emptyTime`                | object | 예        | `{ date, start, end, label }` |
| `options[].key`            | string | 아니요    | `rest` · `pull` · `advance`   |
| `options[].title` · `desc` | string | 아니요    |                               |
| `options[].reason`         | string | 예        |                               |
| `options[].enabled`        | bool   | 아니요    |                               |
| `options[].default`        | bool   | 예        |                               |

`empty-time` 응답은 화면이 읽지 않아요.

#### `POST /api/schedules/voice` · `POST /api/schedules/voice/apply` — 말로 일정 추가 (S6-4)

요청 `text` string 필수. 미리보기 응답: `event` `{ title, date, start, end, repeat, label }` · `moves[].taskId` · `moves[].block` (StudyBlock) · `moves[].pieces[]` `{ date, start, end, minutes, from, to }` · `note`. `apply` 응답은 화면이 읽지 않아요.

#### `GET /api/monthly?month=` · `GET /api/monthly/report?month=`

| 월간 책장 응답                                    | 타입   | null 가능 | 설명                                     |
| ------------------------------------------------- | ------ | --------- | ---------------------------------------- |
| `month` · `today` · `note`                        | string | note 예   |                                          |
| `subjects`                                        | array  | 아니요    |                                          |
| `days[].date`                                     | string | 아니요    |                                          |
| `days[].inMonth` · `today` · `future` · `planned` | bool   | 아니요    | future = 점선 칸 · planned=false = 빈 칸 |
| `days[].plan` · `done` · `rate`                   | number | 아니요    |                                          |
| `days[].bySubject[]`                              | array  | 아니요    | `{ subjectId, pages }`                   |
| `days[].labels`                                   | array  | 아니요    | 총복습 · 시험                            |
| `days[].estimated`                                | bool   | 아니요    | 추정값                                   |

| 리포트 응답                 | 타입   | null 가능 | 설명                                                          |
| --------------------------- | ------ | --------- | ------------------------------------------------------------- |
| `month` · `from` · `until`  | string | 아니요    |                                                               |
| `studyDays` · `plannedDays` | number | 아니요    |                                                               |
| `achievement`               | object | 아니요    | `{ done, plan, rate }`                                        |
| `time`                      | object | 아니요    | `{ measuredMinutes, plannedMinutes }`                         |
| `quiz`                      | object | 아니요    | `{ total, wrong, unsureRight }`                               |
| `subjects[]`                | array  | 아니요    | `{ subjectId, pages, share }`                                 |
| `grades[]`                  | array  | 아니요    | `{ subjectId, unitNo, title, before, after{ grade, score } }` |
| `mockFilled`                | array  | 예 ([])   | mock 표시용                                                   |


## 반복 일정 시간 수정 (FE 누락 → BE ScheduleEditSingleInput/FutureInput 채택)

`PATCH /api/schedules/:id/preview` 요청:

```json
{"scope":"once","expectedRevision":3,"event":{"date":"2026-11-20","start":"18:00","end":"19:00"}}
```

- :id는 `scheduleUUID@원래회차날짜`이며 이동한 화면 날짜로 바꾸지 않는다. calendar.fixed[]에 `revision:integer`를 추가한다. expectedRevision 필수이며 일치하지 않으면409 PREVIEW_STALE.
- scope는 once/future. once의 event는 date/start/end 필수로 그 회차만 이동한다. future의 event는 kind/title/days/startDate/start/end/until 필수이며 days는 중복 없는1..7, until은 종료일 또는 null. startDate는 원래회차날짜 이후이며 days에 맞는 첫 날짜여야 한다. future는 반복 일정에만 허용한다.
- 시간은 같은 날 start<end, 지난 회차는 수정 불가. 다른 고정 일정과 겹치면409 SCHEDULE_CONFLICT. 학습 태스크와 겹치면 기록된 부분을 보존하며 미완료 부분의 이동 후보를 생성한다. 모든 이동은15~60분/시험일/가용시간 규칙을 만족해야 한다.
- future는 원래 규칙을 기준일 전날로 종료하고 새 반복 규칙을 만든다. 이전 예외는 보존한다. 기준일 이후 예외 중 새 규칙에도 원래 회차가 있으면 ID/이동 시각/취소 상태를 유지하고, 없으면 exceptionChanges에서 discard로 설명한다. 실제 반영은 승인 시에만 한다.

응답 data는 `{previewId,event,moves,exceptionChanges,note}`. event는 voice와 같은 표시 객체, moves는 기존 voice의 형식과 같으며 각 block/pieces에 ranges/pages를 포함한다. exceptionChanges는 `[{id,occurrenceDate,action,reason}]`, action=keep|transfer|discard. note는 string|null이다. `PATCH /api/schedules/:id`는 `{previewId}`만 받아 적용 후 `{applied:true}`를 반환한다. 적용 후 캘린더를 다시 조회한다.

삭제 미리보기는 `DELETE /api/schedules/:id/preview?scope=once|future&expectedRevision=3`이다. 미리보기 응답 `{previewId,changes:[{label,before,after}],days:[{date,text}],note}`는 표시용 문자열과 날짜를 사용한다. `DELETE /api/schedules/:id?previewId=…`는 그 후보만 적용하고 기존 emptyTime/options 응답을 반환한다. 요청의 scope를 승인 때 새로 해석하지 않는다.

빈 시간 후보에 `slots:[{date,start,end,label}]`를 추가해 여러 회차의 빈 시간을 모두 표현한다. 기존 emptyTime은 slots의 첫 항목 또는 null이다. rest/pull/advance 후보는 slots 전체를 대상으로 한다. 선택지는 disabled 상태와 사유를 보존하고, 이미 적용한 후보 재요청은 중복 배정을 하지 않는다. 과목 삭제 후 빈 시간 응답에도 같은 형식을 적용한다.

monthly/report.grades[]는 `materialId/unitId`를 포함한다. 월간 달력 칸은 월요일 시작부터 마지막 일요일까지의 실제 주 수에 따라28/35/42칸이다(2월이 월요일 시작인 평년 포함).

### 빈 시간의 현재 시각 경계

실제 날짜를 사용하는 계정의 오늘 빈 시간은 서버 UTC 시계를 사용자 시간대로 바꾼 현재 시각 이후만 제공한다. 23시 이후에는 오늘의 새 공부 후보를 만들지 않는다. `demo_date` 계정은 가상 시각이 없으므로 기존 계획 정책처럼 해당 가상 날짜의 08~23시를 사용한다. 실측·기록 사실은 두 경우 모두 보존한다.
