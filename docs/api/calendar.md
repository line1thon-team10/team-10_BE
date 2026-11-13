# calendar API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- calendar from/to는 양끝 포함, from<=to, 최대42일. 월간 month는 실제 연월, days는 해당 월 달력의 월요일 시작~일요일 종료 35 또는42칸. 모든 날짜는 서버 timezone.
- fixed.id는 일정UUID@원래회차날짜, scheduleId는 일정 UUID. DELETE schedules/:id에는 회차 ID 사용. once는 그 회차 취소 예외, future는 해당 원래 날짜부터 반복 종료. 원본 이전 회차·학습 기록 보존.
- 삭제 후 공부 태스크를 즉시 삭제하지 않는다. 빈 시간이 있으면 {emptyTime,options,previewId}, 없으면 {emptyTime:null,options:[],previewId:null}. rest/pull/advance 모두 반환, 불가능한 선택은 enabled:false.
- empty-time 적용은 option+previewId. rest는 공부 추가 없음, pull은 기존 미래 태스크를 앞당김, advance는 미학습 다음 범위 배정. 날짜·가용 시간·시험일·기록 보존 규칙 준수. 성공 {applied:true}.
- voice는 일정/이동 후보만 저장하며 승인 전 일정/태스크 변경 금지. event는 {title,date,start,end,repeat,label}, moves[].pieces는 실제 분할 범위. apply에 previewId 필수, 성공 {applied:true}.
- study block은 id/subjectId/subjectName/color/title/date/start/end/reason/status가 string, from/to는 integer|null, minutes는 integer, supplement/moved는 bool. exams는 {subjectId,name,examDate,color}[], summary는 과목 ID별 {minutes:integer,count:integer}.
- monthly subjects는 {id,name,color}[], days[].labels는 string[]. plan/done은 고유 페이지 수, rate는 분모0이면0. future에 완료량을 예측하지 않는다. estimated는 실제 측정 시간이 없는 계획 기반 표시에만 사용.
- report는 숫자 집계이며 AI 회고를 생성하지 않는다. from은 해당 월1일, until은 min(today,월말), 미래월은 studyDays=0 등 빈 집계. grades[].before는 {grade:string|null,score:number|null}, after도 동일. share는 과목 학습페이지/전체 학습페이지의 백분율, 0분모면0.

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

#### `DELETE /api/schedules/:id?scope=once|future` → 빈 시간 안 · `POST /api/plan/empty-time`

| 요청                    | 타입   | 필수 | 허용 값                     |
| ----------------------- | ------ | ---- | --------------------------- |
| `scope` (쿼리)          | string | 예   | `once` · `future`           |
| `option` (`empty-time`) | string | 예   | `rest` · `pull` · `advance` |

빈 시간 안 응답 (`DELETE /api/schedules/:id` · `DELETE /api/subjects/:id`)

| 필드                       | 타입   | null 가능 | 설명                          |
| -------------------------- | ------ | --------- | ----------------------------- |
| `emptyTime`                | object | 아니요    | `{ date, start, end, label }` |
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
