# manage API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- 미리보기는 후보만 저장. 적용은 previewId와 서버 revision 검증 후 후보 전체를 한 트랜잭션으로 반영. 클라이언트 계산 changes를 저장에 사용하지 않는다.
- PATCH subjects/:id/preview는 examDate 또는 goal/target 중 하나 이상. target은0..100, examDate는 today 이후. 변경 없으면400 NO_CHANGE. 응답 changes의 before/after는 표시용 string, days는 {date,text}[], note는 string|null, subjectName은 string|null.
- subject card의 examDate는 draft라면 null, dday도 null. range는 {from:integer|null,to:integer|null,pages:integer}; materials[].toc는 {no:integer,title:string,from:integer,to:integer}[]이다. 제거는 status=archived, archive_reason=user_removed이며 기존 학습/퀴즈 이력 삭제 금지.
- additions는 기존 과목/자료에 범위 추가만. 신규 과목은 아래 draft 생성→온보딩→계획 승인으로 지원한다. 1<=from<=to<=실제페이지, 시간 목표라면 minutesPerDay는 양의 정수, days는 중복없는1..7, today<=startDate<=endDate<examDate. 실제 가용시간 넘는 후보는 conflicts에 사유와 leftPages를 반환하고 적용 불가.
- addition summary는 {subjectName:string,materialName:string,from:integer,to:integer,pages:integer,minutesPerDay:integer,totalMinutes:integer,days:integer[],startDate:string,endDate:string}, conflicts는 충돌 수 integer, note는 string|null. placements는 입력 범위를 중복/누락 없이 배치한 조각이며 reason은 string.
- today action은 more30/rest/postpone. postpone에만 taskId 필수. 기록된 범위는 이동하지 않는다. more30은 다음 공부일의 미완료 태스크를 오늘로 앞당긴다. 임의로 기존 태스크 시간을30분 늘리지 않는다. 이동 가능한 미래 태스크가 없으면400 NO_TASK, 오늘 가용시간에 배치할 수 없으면409 PLAN_INFEASIBLE. 적용은 action+previewId; postpone taskId는 후보에서 읽는다. 성공 {applied:true}.
- rebuild는 남은 계획의 재생성 후보를 만든다. 기존501 고정 규칙을 철회한다. 최종 적용 전까지 현재 계획을 유지한다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/subjects` — 과목 카드

| 응답                                                   | 타입   | null 가능 | 설명                            |
| ------------------------------------------------------ | ------ | --------- | ------------------------------- |
| `today`                                                | string | 아니요    |                                 |
| `subjects[].id` · `name` · `color` | string | 아니요 | |
| `subjects[].examDate` | string | 예 | draft 미확정 |
| `subjects[].dday`                                      | number | 예    |                                 |
| `subjects[].goal` · `goalTotal`                        | string | 예        |                                 |
| `subjects[].target`                                    | number | 예        | 목표 점수                       |
| `subjects[].range`                                     | object | 아니요    | `{ from, to, pages }` 시험 범위 |
| `subjects[].progress`                                  | object | 아니요    | `{ studied, total, rate }` 진도 |
| `subjects[].readiness`                                 | number | 예        |                                 |
| `subjects[].removed` · `archived`                      | bool   | 예        | 빠짐 · 시험 끝나 자동 보관      |
| `subjects[].materials[].id` · `name` · `pages` · `toc` | –      | 아니요    |                                 |
| `subjects[].materials[].mock`                          | bool   | 예        | mock 표시용                     |

#### `PATCH /api/subjects/:id/preview` → `PATCH /api/subjects/:id` · `DELETE /api/subjects/:id/preview` → `DELETE /api/subjects/:id`

| 요청 (`PATCH …/preview`) | 타입   | 필수     | 설명      |
| ------------------------ | ------ | -------- | --------- |
| `examDate`               | string | 시험일만 |           |
| `goal`                   | string | 목표만   |           |
| `target`                 | number | 목표만   | 목표 점수 |

- 미리보기 응답(두 preview 공통 · `POST /api/plan/today/preview`도 같음): `subjectName` (null 가능) · `changes[]` `{ label, before, after }` · `days[]` `{ date, text }` (null 가능) · `note`
- `PATCH /api/subjects/:id` 응답: 과목 카드 한 개 (위 `subjects[]`와 같은 모양)
- `DELETE /api/subjects/:id` 응답: 빈 시간이 생기면 빈 시간 안(`emptyTime` · `options`), 아니면 `emptyTime` 없음

#### `POST /api/plan/additions/preview` → `POST /api/plan/additions` — 계획 추가 (S13)

| 요청                    | 타입     | 필수 | 설명    |
| ----------------------- | -------- | ---- | ------- |
| `subjectId`             | string   | 예   |         |
| `materialId`            | string   | 예   |         |
| `from` · `to`           | number   | 예   | 쪽 범위 |
| `minutesPerDay` | integer | 둘 중 하나 | 하루 목표 분 |
| `totalPages` | integer | 둘 중 하나 | 총 목표 페이지 |
| `days`                  | number[] | 예   | 요일    |
| `startDate` · `endDate` | string   | 예   |         |

미리보기 응답: `previewId` · `summary` · `placements[]` `{ date, start, end, from, to, minutes, reason }` · `conflicts` · `leftPages` · `note`. 적용 응답은 화면이 읽지 않아요.

#### `POST /api/plan/today/preview` → `POST /api/plan/today` — 오늘 계획 (PL-10) · `POST /api/plan/rebuild`

| 요청     | 타입   | 필수       | 허용 값                        |
| -------- | ------ | ---------- | ------------------------------ |
| `action` | string | 예         | `more30` · `rest` · `postpone` |
| `taskId` | string | postpone만 |                                |

미리보기 응답은 위 미리보기 모양, 적용 응답은 화면이 읽지 않아요. `rebuild`의 요청·응답은 아래 재생성 계약을 따른다.

---

## 신규 과목 추가 (PDF pp.13,17 / BE SubjectInput·SubjectAddChangeInput)

`POST /api/subjects`는 `{name:string}`으로 draft 과목을 만든다. 빈 이름은400 EMPTY. 서버가 ID/색을 정하고 `{subjectId,status:"draft"}`를 반환한다. 기존 활성 계획에는 즉시 추가하지 않는다.

1. 반환 subjectId로 [온보딩](onboarding.md)의 GET/message를 호출해 시험·목표4칸을 확인한다. 시험일/목표가 미수집이면 null로 표시한다.
2. 해당 과목에 여러 자료를 업로드하고 자료별 단원/개념 범위를 저장한다.
3. `GET /api/plan/draft?draftSubjectIds=id1,id2`로 기존 과목의 남은 범위와 신규 과목을 함께 재배분한 후보를 받는다. draftSubjectIds는 중복 없는 쉼표 구분 ID 목록이다. 다른 사용자/이미 활성 과목은 거절한다.
4. S5에서 확인 후 `POST /api/plan/approve`에 `{previewId}`를 전달한다. 후보에 담긴 과목만 활성화하고 기존 기록·점수·책·보충을 보존한다. 취소/이탈은 기존 계획을 바꾸지 않는다. 재접속 시 draft는 재사용 가능하다.

GET subjects의 카드에 `status:"draft"|"active"|"archived"`를 추가한다. examDate/dday/goal/goalTotal/target은 미확정이면 null. range는 `{from,to,pages}`이며 다중 자료/비연속이면 from/to=null, pages는 자료별 시험 범위 합집합이다. materials[].toc에는 `unitId`를 추가한다. 시험 종료 자동 보관은 시험일 당일이 아닌 **다음 날** 수행한다(와이어 S14).


## 범위 추가의 목표 선택 (와이어 S13)

additions/preview 요청은 subjectId/materialId/from/to/days/startDate/endDate와 **minutesPerDay 또는 totalPages 중 정확히 하나**다. 총 페이지 목표는1..선택 범위 페이지 수, 선택 범위의 첫 페이지부터 그 수만큼 배정한다. 시간 목표는 선택 범위 전체를 해당 일일 목표 내에서 배정한다. 범위 추가는 해당 과목만 재배분하고 다른 과목 계획은 보존한다.

summary의 minutesPerDay는 총 페이지 모드에서 null, totalPages는 실제 목표 페이지 정수, goalType은 time/pages이다. placements[]에는 materialId/unitId/ranges/pages를 추가하고 from/to는 단일 범위일 때만 값이 있다. conflicts>0 또는 leftPages>0이면 적용을 허용하지 않고409 PLAN_INFEASIBLE. 성공 적용 응답은 `{applied:true}`다.


## 재생성 (PDF p.13 / BE RegenerateChangeInput)

`POST /api/plan/rebuild` body `{request?:string,saved?:boolean}`. request 생략은 남은 전체 범위 재생성, saved 기본 false. 현재 계획이 없으면400 NO_PLAN. 응답 `{previewId,ready:true,question:null}` 뒤 `GET /api/plan/resplit?previewId=…`로 기존 S5-2 비교 형식을 조회한다. 승인 `POST /api/plan/resplit/apply {previewId}`, 취소 `DELETE /api/plan/resplit?previewId=…`. 기록된 페이지·점수·책·보충을 삭제하지 않는다.


## 오늘 계획·목표 적용 시점 (PDF p.13)

more30은 기존 경로/enum을 유지하되 **다음 예정 태스크를 오늘로 이동**한다. 후보 선택은 미래 날짜→시작 시각→taskId 순서이며 기록된 범위는 제외한다. 원래 태스크 전체를 옮길 자리가 없으면 계획 불가를 반환한다. 화면은 실제 이동 분량/시간을 보여 주며 '정확히30분 증가'라고 단정하지 않는다. rest는 오늘 미완료 범위를 남은 일정에 재배분, postpone은 해당 task를 다음 과목 공부일의 첫 순서로 이동한다. 모두 미리보기 후 승인한다.

과목 goal/target 변경은 다음 날부터 배분 가중치에 반영한다. examDate 변경은 남은 일정 후보를 재계산한다. 과목 삭제는 archive이며 기록을 지우지 않는다. 날짜·목표 동시 변경도 하나의 후보로 검증한다.


## 과목 제거의 이력·의무 보존

draft 과목은 승인 계획 없이도 제거 후보를 확인하고 보관할 수 있다. 승인된 계획의 제거는 계획 revision을 갱신한다. running/paused 또는 종료했지만 기록하지 않은 집중 세션이 있으면409 ACTIVE_SESSION이며 먼저 세션 기록을 마쳐야 한다.

제거 승인 후 status=archived/archive_reason=user_removed로 계획 소속을 끝낸다. 미시작 미래 study/review 배치는 excluded로 남기고, 보충 태스크는 원래 상태·범위를 그대로 보존한다. 보충을 완료한 것으로 바꾸거나 회독·회복 보너스를 주지 않는다. 이후 today/calendar/allocator에서는 보관 과목의 예정 작업을 제외하지만 과거 기록·점수·책과 월간 완료 사실은 유지한다.

빈 시간 선택 후보는 제거와 같은 트랜잭션에서 새 계획 revision을 기준으로 만든다. 후속 후보 생성 실패는 제거 전체를 되돌린다. 같은 삭제 previewId 재요청은 최초 빈 시간 응답을 재생한다. 빈 슬롯이 없으면 `{emptyTime:null,slots:[],options:[],previewId:null}`이다. 데모의 보관 기준일은 적용된 subject_archive 후보의 base_date에 고정되어 날짜 이동 후에도 변하지 않는다.
### 범위 추가의 저장·배분 경계 (2026-11-14)

- 선택 범위는 현재 시험 범위와 합집합으로 반영하며, 이미 계획된 미학습 페이지는 새 복제 태스크로 더하지 않고 해당 과목의 남은 범위에 한 번만 배치한다. 이미 학습했거나 집중을 시작한 페이지는 기록·고정 배치로 충족하며 변경하지 않는다. summary의 목표 페이지 수는 요청대로 유지하고 placements에는 남은 부분만 표시한다. leftPages는 미학습·미배정 잔량이며 이미 충족한 페이지를 불가능 분량에 포함하지 않는다.
- 목차 밖의 실제 PDF 페이지를 추가하면 근거 없는 수동 단원을 만들고, 기존 단원 내부의 제외 범위를 부분적으로 추가하면 선택 부분만 개념 범위로 확장한다. 기존 개념·기록·점수 이력은 삭제하지 않고 자료 구조 편집과 같은 retirement 규칙을 따른다. 선택하지 않은 제외 페이지를 함께 활성화하지 않는다.
- `days/startDate/endDate/minutesPerDay`는 선택한 추가 페이지의 예상 분량에만 적용한다. 기존 미완료 페이지는 원래 시험 전 가용 시간에 배분하며, 짧은 추가 범위는 같은 과목의 기존 범위와 한 태스크로 합쳐 15~60분을 지킨다. placements의 각 항목은 실제 태스크에서 추가 범위가 차지하는 조각이며, 조각 자체가 15분 미만이어도 최종 태스크는 15분 이상이다.
- 불가능한 미리보기는 `conflicts>0`, 미학습·미배정 잔량인 `leftPages`, 빈 placements 및 원인을 보관한다. 부분적으로 배정한 결과를 성공으로 취급하지 않으며 해당 preview 승인은 `409 PLAN_INFEASIBLE`다. 탐색 자원 한도는 별도 `503 PLAN_SEARCH_LIMIT`이며 불가능 판정으로 저장하지 않는다.
