# plan API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- GET draft는 준비된 자료·확정 목표·현재 일정으로 후보를 생성/재사용한다. 승인된 plans/study_tasks를 수정하지 않는다. 후보 저장은 허용하지만 GET 호출만으로 계획을 승인하지 않는다.
- 태스크는 15~60분, 총 페이지를 빠뜨리거나 중복 배정하지 않는다. 나눌 수 없는 마지막 15분 미만 잔여는 별도 짧은 태스크 허용하고 reasons에 설명한다. 시험 당일 이후에는 해당 과목 배정 금지, 하루 가용 시간 초과 금지. 불가능하면 409 PLAN_INFEASIBLE와 한국어 사유를 반환한다.
- totalPages는 현재 inExam 범위 합집합, taskCount는 실제 후보 태스크 수. dailyAvgMinutes는 공부가 배정된 날짜의 평균을 반올림, daysToFirstExam은 today와 첫 시험의 날짜 차이. firstWeek는 today부터 7일의 {date:string,minutes:{[subjectId]:integer}} 배열이다. minutes는 과목별 분량 map이며 총분량 scalar가 아니다.
- approve는 최신 draft의 previewId를 body로 받는다(이번 보완). 성공 {approved:true}. 이미 승인한 같은 후보는 같은 응답, 다른 최신 후보는 revision 검증. 하나의 사용자 승인 계획만 유지한다.
- resplit POST는 request 필수(빈 문자열은 다른 버전 요청), answer 선택, saved 기본 false. 후속 질문은 최대 한 번. 질문이면 {question:{text,choices},ready:false}, 후보 완성이면 {question:null,ready:true,previewId}.
- GET resplit의 days[].before/after는 **integer 분량(분)**이며 배열이 아니다. stats before/after는 number, label/unit string. summary 총시간은 실제 태스크 합산값이며 예시 1025/1005에 맞추지 않는다.
- apply는 previewId 필수, 반환 {applied:true}. 최초 계획 승인 전이라면 draft만 교체하고 승인된 계획은 건드리지 않는다. DELETE는 후보만 취소하고 {cancelled:true}. 승인 후 재분할은 같은 preview/revision 규칙으로 승인 계획에 원자적 적용한다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/plan/draft` (`?saved=1`) · `POST /api/plan/approve` — S5 분량

| 응답                                        | 타입   | null 가능 | 설명                                     |
| ------------------------------------------- | ------ | --------- | ---------------------------------------- |
| `saved`                                     | bool   | 예        | 저장된 결과                              |
| `today`                                     | string | 아니요    |                                          |
| `summary`                                   | object | 아니요    | 위 S5 절 (`totalPages` · `taskCount` 등) |
| `allocation[].subjectId` · `name` · `color` | string | 아니요    |                                          |
| `allocation[].goalLabel`                    | string | 예        | 목표 표시 (예 `A+`)                      |
| `allocation[].pages` · `tasks`              | number | 아니요    |                                          |
| `allocation[].units[]`                      | array  | 아니요    | `no` · `title` · `pages` · `tasks`       |
| `firstWeek`                                 | array  | 아니요    | 위 S5 절                                 |
| `reasons`                                   | array  | 예        | 배분 이유                                |

`POST /api/plan/approve` 응답은 화면이 읽지 않아요 (성공만 봄).

#### `POST/GET/DELETE /api/plan/resplit` · `POST /api/plan/resplit/apply` — S5-1 · S5-2

| 요청 (`POST`) | 타입   | 필수 | 설명                |
| ------------- | ------ | ---- | ------------------- |
| `request`     | string | 예   | 사용자 요청 문장    |
| `answer`      | string | 선택 | 후속 질문에 고른 답 |
| `saved`       | bool   | 선택 | 저장된 결과로       |

| 응답 (`POST`)      | 타입     | null 가능 | 설명                             |
| ------------------ | -------- | --------- | -------------------------------- |
| `question.text`    | string   | 예        | 후속 질문 (없으면 비교 화면으로) |
| `question.choices` | string[] | 예 ([])   |                                  |

| 응답 (`GET` 비교)                             | 타입   | null 가능 | 설명       |
| --------------------------------------------- | ------ | --------- | ---------- |
| `request` · `keep`                            | string | 아니요    | 위 S5-2 절 |
| `saved`                                       | bool   | 예        |            |
| `days[].date`                                 | string | 아니요    |            |
| `days[].before` · `after`                     | array  | 아니요    |            |
| `days[].changed`                              | bool   | 예        |            |
| `stats[].label` · `unit` · `before` · `after` | –      | 아니요    |            |
| `reasons`                                     | array  | 예 ([])   |            |

`apply` · `DELETE` 응답은 화면이 읽지 않아요.
