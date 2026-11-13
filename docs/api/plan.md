# plan API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- GET draft는 준비된 자료·확정 목표·현재 일정으로 후보를 생성/재사용한다. 신규 과목 추가는 manage.md의 draftSubjectIds를 받는다. 승인된 plans/study_tasks를 수정하지 않는다. 후보 저장은 허용하지만 GET 호출만으로 계획을 승인하지 않는다.
- **PL-02, 원문 p.16:** 모든 태스크는 15~60분이며 총 페이지를 빠뜨리거나 중복 배정하지 않는다. 이전 초안의 15분 미만 예외는 철회한다. 잔여는 인접 범위와 합쳐 재분할하고, 원천적으로 불가능하면 PLAN_INFEASIBLE로 설명한다. 시험 당일 이후에는 해당 과목 배정 금지, 하루 가용 시간 초과 금지. 불가능하면 409 PLAN_INFEASIBLE와 한국어 사유를 반환한다.
- **PL-04/4-7, 원문 p.12:** 시험 직전일은 해당 과목80%/다른 과목20%, 시험일 역순으로 직전일 우선 예약. 직전일이 다른 시험날이면 D-2로 앞당긴다. 배분 점수는 목표 가중치×시험 임박도×남은 분량×(1-숙달도). 각 항의 상세 수치 함수는 원문에 없으므로 별도 미확정 계산 정책이며 임의로 확정하지 않는다.
- totalPages는 materialId별 현재 inExam 범위 합집합, taskCount는 실제 후보 태스크 수. dailyAvgMinutes는 공부가 배정된 날짜의 평균을 반올림, daysToFirstExam은 today와 첫 시험의 날짜 차이. firstWeek는 today부터 7일의 {date:string,minutes:{[subjectId]:integer}} 배열이다. minutes는 과목별 분량 map이며 총분량 scalar가 아니다.
- approve는 최신 draft의 previewId를 body로 받는다(이번 보완). 성공 {approved:true}. 이미 승인한 같은 후보는 같은 응답, 다른 최신 후보는 revision 검증. 하나의 사용자 승인 계획만 유지한다.
- resplit POST는 request 필수(공백/빈 문자열은400 EMPTY), answer 선택, saved 기본 false. 후속 질문은 최대 한 번. 질문이면 {question:{text,choices},ready:false}, 후보 완성이면 {question:null,ready:true,previewId}.
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
| `allocation[].units[]` | array | 아니요 | `unitId` · `materialId` · `no` · `title` · `pages` · `tasks` |
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
| `days[].before` · `after`                     | integer  | 아니요    |            |
| `days[].changed`                              | bool   | 예        |            |
| `stats[].label` · `unit` · `before` · `after` | –      | 아니요    |            |
| `reasons`                                     | array  | 예 ([])   |            |

`apply` · `DELETE` 응답은 화면이 읽지 않아요.


## 후보와 후속 질문 식별

- GET draft 및 준비 완료된 resplit 비교 응답에 `previewId:string` 필수. summary는 `{totalPages,taskCount,dailyAvgMinutes,daysToFirstExam}`이며 모두 정수다. allocation.units[]의 unitId/materialId도 필수다.
- POST resplit의 request는 공백이 아닌 string, saved는 기본 false. 한 번의 추가 질문이 필요하면 `{ready:false,question:{text,choices},requestId}`를 반환한다. 후속 요청은 같은 request와 `{requestId,answer}`를 보내며 requestId 없는 answer는422 INVALID_INPUT. 준비가 끝나면 `{ready:true,question:null,previewId,requestId}`를 반환한다. requestId는 string이며 사용자별 요청을 구분한다.
- GET/DELETE resplit은 `?previewId=…`로 비교/취소 대상을 명시한다. FE의 기존 ID 없는 조회는 해당 사용자의 후보가 정확히 하나일 때만 허용하고, 여러 후보면409 PREVIEW_REQUIRED. DELETE는 명시적인 previewId가 필수다. 취소된 후보는 적용할 수 없다.
- POST resplit/apply와 plan/approve는 `{previewId}`만 받는다. 신규 과목/기존 계획 재생성/초기 계획 후보의 종류는 서버가 저장한 후보에서 결정한다. 초기 resplit/apply는 draft만 바꾸므로 S5의 approve까지 마쳐야 첫 계획이 활성화된다.
- 모든 후보는 [버전·날짜 검증](decisions.md)을 따른다. 입력/학습 기록이 바뀌면 새 후보를 요청한다. 표시용 days/stats 숫자를 적용 요청으로 보내지 않는다.
