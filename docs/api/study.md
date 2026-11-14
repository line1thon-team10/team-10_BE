# study API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- GET today는 승인 계획 없으면 404 NO_PLAN. tasks는 오늘의 현재 planned/partial/completed/missed, excluded/carried_over 과거 배치는 제외한다. notice/paceAlert/replan은 없으면 null, 배열은 빈 배열.
- 기록 result는 done/partial/none. done은 task.ranges 전체 완료, none은 0쪽. partial은 아래 범위별 기록 계약을 따른다. 범위 없는 총복습 태스크에는 partial 금지(400 BAD_PAGE).
- 같은 task의 기록 수정은 기존 기록/범위를 교체하고 모든 집계를 재계산한다. 별도 회독이나 책을 중복 생성하지 않는다. 과거 기록의 study_date는 보존한다. 범위 밖 toPage는 clamp하지 않고 거절한다.
- sessionId는 같은 사용자/태스크의 ended 또는 auto_stopped 세션만. 다른 기록에 사용됐으면 409 SESSION_USED. 동일 기록에 동일 세션을 재전송하면 허용한다. 기록에 minutes 입력은 받지 않는다.
- PATCH record는 기존 기록이 있으면 done/partial/none 모두 정정할 수 있다. 기록이 없으면 400 NOT_RECORDED다. 완료량에 따라 회독/블록·진도를 재판정하되 배치가 이미 carried_over/excluded이면 그 상태를 보존한다. 과거 마감 분모나 기존 자식 배치를 바꾸지 않고 배치·학습 의무를 자동 추가하거나 부활시키지 않는다.
- interpret는 text를 페이지 후보로만 해석, 기록 저장 없음. 모호하거나 범위 밖이면 400 PARSE_FAILED.
- **BK-03/4-5, 원문 pp.2,11,19:** P1 dayClose는 (오늘 모든 태스크 기록 또는 서버 현지시각23:30 이후)이고 진행률>=70일 때 사용자/날짜당 한 번. 일반 기록은 toast 또는 event:null. 중복 제출로 박스를 재발급하지 않는다.
- 이월은 기존 배치를 carried_over로 보존하고 같은 block_id로 새 배치를 만든다. 미완료 페이지는 합집합으로 계산. **RC-02/4-8, 원문 p.12:** 과목별 누적 밀린 페이지 >= ceil((그 과목 전체 범위 페이지 / 그 과목 태스크가 있는 날 수)×0.5)이면 replan, 미만이면 다음 공부일로 carry. 남은 날 평균이나 과목 합산 기준으로 대체하지 않는다. 예:140/8=17.5, 임계9쪽;8쪽은 carry,9쪽은 replan. 미완료가 0이면 none, 남은 계획일이 0이면 배정 불가 안내.
- replan A는 가용 시간 내 추가 배정, B는 미학습·비강조 범위를 제외, C는 남은 범위를 압축 복습. **RC-05:** 보충 태스크는 삭제하거나 범위 축소하지 않고 날짜만 조정한다. 기록된 범위를 삭제하지 않는다. [나중에]는 후보/알림을 제거하지 않으며 다음 접속 때 다시 표시한다(RC-04). 불가능한 안은 enabled:false. 세 안을 항상 반환하되 모두 불가능해도 거짓 실행안을 만들지 않는다.
- GET replan/options는 {options,saved,previewId}. POST replan은 key A/B/C와 previewId. 적용 {event:{type:"box",reason:"recover",...}}. 선택한 disabled 안은 400 BAD_KEY.
- 속도 보정은 최근 3일의 유효 종료 측정 시간/실제 완료 페이지로 계산하며 autoStopped/excluded는 제외. **PL-07/4-11, 원문 pp.12,16:** 3일 측정 후 현재 기준 속도와20% 이상 차이일 때 과목당 하루 한 번 제안한다. 샘플/조건 부족이면 paceAlert:null. apply:false는 제안만 해제, true는 승인 속도와 미래 예상 시간 갱신; 일정 초과 시 replan=true. P2가 P4보다 먼저 표시된다.

## 속도 보정의 단위와 샘플

원문 p.23에서 미정이던 난이도/속도 관계를 POL-01 실행 중 확정했다. `minutes_per_page`는 난이도 배수가 제거된 기준 속도 b이며 초기2.5다. 각 샘플의 유효 종료 측정 분 t, 실제 완료한 고유 쪽수 p, 측정 시작 당시 난이도 배수 m을 사용한다. 복합 범위는 자료별 중복 페이지를 제거하고 서로 다른 배수의 페이지를 별도로 합산한다. 같은 페이지가 여러 개념에 걸쳐 다른 배수가 있다면 해당 단원의 배수를 한 번만 사용한다.

- 화면 `perPage = Σt / Σp`, `expectedPerPage = b × Σ(p×m) / Σp`로 동일 샘플의 실제/예상 속도를 비교한다. `abs(perPage / expectedPerPage - 1) >= 0.20`일 때만 제안한다. 판정 전에 표시값을 반올림하지 않는다.
- 저장할 `proposed_minutes_per_page = Σt / Σ(p×m)`다. 이후 태스크의 유효 분/p는 이 값×해당 난이도다. 예: high(m=1.5)를3.75분/p에 공부했다면 b=2.5 그대로이며 알림을 만들지 않는다.
- 과거 샘플의 난이도는 배정/측정 시점 snapshot으로 확인한다. 확인할 수 없는 legacy 데이터, 페이지0, 시간0, 정정으로 페이지 근거가 사라진 기록은 보정에서 제외한다. 새 학습·보충의 서로 다른 실제 측정은 개별 샘플로 계산하되 같은 session/record를 중복 소비하지 않는다.
- 현재 승인 b와 최근3일 샘플을 기준으로 계산하며 저장한 제안에는 샘플/계획/속도 revision을 고정한다. 3일 조건, 과목당 하루 한 번, 제외 세션, P2 우선순위와 승인 시점 검증은 위 규칙을 유지한다.

## 집계와 누락된 중첩 형식

- **와이어 S6·S15:** 홈/일별 달성률은 그날 계획된 새 학습+보충의 task별 완료량/계획량이다. 동일 task 안 중복 범위는 제거하지만 새 학습 task와 보충 task를 같은 페이지라는 이유로 하나로 합치지 않는다. 과목 진도율의 고유 새 범위 집계와 구분한다.
- rate는 min(100, HALF_UP(100*donePages/totalPages)), 분모0이면0. 판정은 반올림 전 비율로 한다. quiz.remainPages=max(0,ceil(.7*출제가능계획페이지)-출제가능완료페이지). 일반 progress와 quiz용 분모를 구분한다.
- Task의 페이지 수는 서버가 반환한 pages를 사용한다. from/to/unitNo는 단일 범위의 표시용이며 복합 범위는 null이다. FE에서 to-from+1로 집계하지 않는다.
- replan options[].lines는 string[], effect/preview는 string, enabled는 bool. options 응답은 표의 전체 replan 객체에 previewId를 더한 형태이다(앞 규칙의 {options,saved,previewId}는 요약).
- paceAlert에 calibrationId:string을 추가한다. POST pace는 subjectId/apply/calibrationId; 반환 {applied:bool,overDays:string[],replan:bool}. rows[].now는 기존 예상 분 정수, rows[].date는 날짜, 나머지 페이지 필드는 정수.

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
| `unitNo`           | number | 예        | 단일 단원의 표시 번호 |
| `title`            | string | 아니요    |                                        |
| `from` · `to`      | number | 예        | 단일 범위 표시용; 복합 범위 null |
| `pages` | integer | 아니요 | 서버가 ranges의 materialId별 합집합으로 계산 |
| `ranges` | Range[] | 아니요 | 아래 범위 계약 |
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
| `toPage` | integer | 선택 | 단일 범위 partial의 호환 입력 |
| `completedRanges` | array | 선택 | 복합 범위 partial에 필수; 아래 계약 |
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
| `toPage` | integer | 선택 |
| `completedRanges` | array | 선택 |

toPage 또는 completedRanges 중 정확히 하나를 보낸다. 응답: `taskId` string · `status` string (Task status) · `doneTo` integer|null · `donePages` integer · `ranges` Range[] · `rate` number

#### `POST /api/records/interpret` — 말로 기록 (AI)

| 요청     | 타입   | 필수 |
| -------- | ------ | ---- |
| `taskId` | string | 예   |
| `text`   | string | 예   |

응답: `from/to` integer|null · `completedRanges` array · `label` string. 복합 범위는 from/to=null. 해석 결과를 확인한 후 records 요청으로 저장한다.

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

## 범위별 기록 계약 (PDF-01, PDF-08 / BE PageTaskRange 채택)

모든 Task는 `ranges:[{id,materialId,unitId,conceptId,from,to,doneTo}]`를 반환한다. ID는 string, conceptId는 string|null, from/to는 실제 PDF의 양끝 포함 정수, doneTo는 해당 범위에서 완료한 마지막 페이지 또는 null이다. 범위 순서는 서버 배치 순서다. 같은 PDF 안 중복 페이지는 pages/donePages에서 한 번만 센다. 다른 PDF의 같은 쪽 번호는 별개다. `ranges:[]`이면 pages=0이며 완료 여부는 result로 기록한다.

```json
{"taskId":"task-id","result":"partial","completedRanges":[{"rangeId":"range-a","toPage":5},{"rangeId":"range-b","toPage":2}]}
```

completedRanges는 해당 task 범위의 처음부터 toPage까지 완료했다는 **전체 교체 스냅샷**이다. 생략된 range는 미완료로 간주한다. partial에는 한 쪽 이상, 전체보다 적은 완료량이 필요하다. 모든 범위 완료는 done, 0쪽은 none으로 보낸다. 중복/타인 rangeId와 범위 밖 페이지는 `422 INVALID_INPUT`, `toPage`와 completedRanges 동시 전송은 거절한다. 단일 범위에 한해 기존 toPage를 허용한다. PATCH 정정에도 같은 교체 의미를 적용하되 결과가 전체/0쪽이면 서버가 done/none으로 산정한다.

home notice.items와 paceAlert.rows에도 대상 `ranges`와 서버 계산 `pages`를 반환한다. FE는 범위 선택·자료 열기에서 materialId를 보존한다. 이월 및 계획 재배치로 task ID가 바뀌어도 기존 학습 범위를 중복 완료 처리하지 않는다.

### P1의 조회 시 발급

`GET /api/today`의 `data.event`는 기록 응답과 같은 P1 event 또는 null이다.
23:30 이후 추가 기록 없이 홈을 조회해도 실제 완료비율70% 이상이면 한 번 발급한다.
POST records와 GET today는 같은 사용자/현지날짜의 발급 기록을 공유한다.
Idempotency-Key를 붙인 기록 재전송은 최초 응답을 재생하지만 새 알림 행은 만들지 않는다.
발급 판정은 반올림 전 일반 계획 달성률이며, 퀴즈 열림 안내는 출제 가능한 범위의 별도 비율로 판단한다.
시험용 demo_date는 날짜만 대체하며23:30 경계 시각은 사용자 시간대의 서버 시각을 사용한다.

### 기록 해석의 원문 검증

`records/interpret`는 확정 기록을 쓰지 않는다. 모델 해석 전후로 사용자 소유 태스크와 실제 PDF 범위를 검증하며, AI 호출 동안 기록·태스크·자료·계획 또는 기준 날짜가 바뀌면 `409 INPUT_STALE`로 후보를 폐기한다. 기존 기록과 합산하지 않고 이번 발화의 전체 `completedRanges` 후보를 반환한다.

- 명시한 `13쪽까지`, `10~15쪽`, `10페이지부터 15페이지까지`에 완료 표현이 있을 때 범위 시작부터의 접두 범위로 해석한다. `5쪽 읽었어`처럼 분량인지 끝 페이지인지 구분할 수 없는 표현, 일부만 말한 중간 구간, 추정·예정·질문은 `400 PARSE_FAILED`다.
- 여러 PDF/태스크 범위에 같은 페이지가 있으면 자료 파일명이나 단원 제목으로 대상을 구별해야 한다. 한 자료를 모두 읽었다는 말로 다른 자료까지 완료하지 않는다. 범위별 발화는 쉼표·줄바꿈·`그리고`로 구별할 수 있다.
- `다 못 했어`는 0쪽과 같지 않으므로 거절한다. 명시적 미완료는 빈 후보, 명시적 전체 완료는 해당 범위의 끝까지를 제안한다. 실제 기록은 사용자 확인 후 별도의 `POST /api/records`로 저장한다.
- 구조가 유효해도 모델이 원문 페이지/범위를 바꾸거나 누락하면 `503 AI_FAILED`다. 외부 모델 실패나 키 누락도 동일하며 성공으로 꾸미지 않는다. 페이지가 없는 태스크는 페이지 후보로 변환하지 않는다.
