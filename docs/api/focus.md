# focus API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- 사용자당 running/paused 세션 최대 하나. 같은 task로 start 재호출은 기존 세션, 다른 task는 409 OTHER_ACTIVE. task는 현재 사용자·오늘·활성 계획 소속이어야 한다.
- active는 없으면 data:null. API status는 active/paused/ended; DB running→active, auto_stopped→ended+autoStopped:true.
- pause: active→paused, 반복 pause는 같은 상태 반환. resume: paused→active, active+checkIn은 확인 시점 갱신, 일반 active는 그대로. 종료 상태 resume은 409 ENDED. end는 active/paused→ended, 반복 end는 같은 종료 결과 반환.
- minutes/pausedMinutes는 서버 timestamp의 구간 합을 분 단위 내림한 정수. 열린 구간은 조회 시각까지. 음수 구간 금지. 세그먼트는 자정을 가로지를 수 있고 todayMinutes는 현지 날짜 경계로 자른다.
- 예상 시간 2배가 되면 checkIn:true. 알림 후 10분 무응답이면 알림 시점에 자동 종료, autoStopped/excludeFromPace=true. 조회가 늦어도 조회 시각까지 시간을 부풀리지 않는다.
- material은 자료가 없으면 null, 있으면 {id:string,page:integer}. 여러 자료면 task.ranges의 첫 범위를 기본으로 열며 다른 자료 선택은 ranges로 제공한다. recorded는 연결된 study_record 존재 여부. quote는 세션 생성 때 정해 고정한다.

## 구현 완료 및 FE 전달

`feat/focus-contract`는 요청마다 사용자 잠금을 얻고 상태를 다시 읽는다. GET도 자동 종료 정산이 가능하므로 reset/seed/next-day와 같은 잠금을 사용한다. 경합 중에는 `409 REQUEST_IN_PROGRESS`; PostgreSQL은 타 사용자와 독립적이고 SQLite는 DB 쓰기 잠금 때문에 타 사용자도 일시 충돌할 수 있다.

- taskId/경로 session_id는 UUID이며 형식 오류는422, 없는/타인 세션은404 SESSION_NOT_FOUND다. start는 `{taskId}`만 받고 pause/resume/end는 본문 생략 또는 `{}`를 받는다. minutes 같은 미정의 필드는422다.
- 새 start는 오늘의 활성 과목·현재 사용자 계획에 속한 planned/partial 태스크만 받는다. 완료는409 ALREADY, 제외/이월/날짜 불일치는404 TASK_NOT_FOUND다. 기존 BE의 미등록 TASK_DONE을 ALREADY로 보정했다. 이미 진행 중인 같은 태스크의 재진입은 자정 이후에도 같은 세션을 반환한다.
- 반복 pause는 paused 유지, 일반 active resume은 확인 기준 불변. checkIn 중 pause/resume만 현재 순공부 초로 확인 기준을 갱신한다. 종료 상태 pause는409 NOT_ACTIVE, resume은409 ENDED다. 종료 재전송은 종료 시각/측정값을 바꾸지 않는다. recorded와 오늘/과목 합계는 현재 상태에 따라 갱신될 수 있다.
- `expected_seconds`와 `quote`는 세션 생성 시 저장한다. task.minutes는 그 세션의 예상 시간 스냅샷이다. 계획의 예상 시간을 나중에 수정해도 진행 중인 확인 기한이 바뀌지 않는다. 기한 판정과 구간 쓰기는 같은 요청 시각으로 수행하고 초는 내림한다.
- 일반 todayMinutes는 사용자 시간대 현지 자정 구간과 각 순공부 구간의 교집합이다(DST 포함). **데모는 실제 시계와 가상 날짜가 다르므로 study_date에 귀속**한다. 오늘 합계는 모든 과목, 과목 합계는 해당 과목의 모든 날짜이며 현재 열린/paused 세션도 포함한다. 다른 사용자 값은 합산하지 않는다.
- task.ranges는 [study 범위](study.md) 형식이다. 동일 PDF의 중복 페이지는 한 번, 다른 PDF의 같은 쪽은 별개다. 복합 범위의 from/to는 null. 자료/범위가 없으면 material:null, ranges:[], pages:0이다. 보관 과목·retire된 단원도 본인의 과거 세션 조회에 포함한다.
- ERD의 '활성 중 seed 거절' 메모는 승인된 42분 reset 시연과 충돌하여 정정했다. **reset/seed는 잠금 아래 교체 허용, next-day는 만료를 먼저 정산하고 실제 running/paused일 때만 SESSION_RUNNING**이다. active GET을 먼저 호출해야 날짜가 넘어가는 기존 BE 결함을 보정했다.

FE 경로는 유지한다. material null 분기, 복합 범위 표시, 서버의 todayMinutes/subjectMinutes 사용, REQUEST_IN_PROGRESS 재시도 안내가 필요하다. 기존 FE mock의 일반 active resume이 기준을 갱신한다면 위 계약대로 맞춘다. 범위 전체 계약은 학습 기록 단계와 연결되며 이 PR이 기록/퀴즈의 모든 명세를 완성했다는 뜻은 아니다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `POST /api/study/sessions` · `GET /api/study/sessions/active` · `GET /api/study/sessions/:id` · `POST …/:id/pause|resume|end`

| 요청 (`POST /sessions`) | 타입   | 필수 |
| ----------------------- | ------ | ---- |
| `taskId`                | string | 예   |

응답 (여섯 경로 같음, `active`는 진행 중이 없으면 `null`)

| 필드                                          | 타입   | null 가능 | 설명                                     |
| --------------------------------------------- | ------ | --------- | ---------------------------------------- |
| `id` · `taskId` · `subjectId` · `subjectName` | string | 아니요    |                                          |
| `task.title`                                  | string | 아니요    |                                          |
| `task.from` · `to` | integer | 예 | 복합 범위 null |
| `task.pages` | integer | 아니요 | 서버 계산 |
| `task.ranges` | Range[] | 아니요 | study.md의 범위 형식 |
| `task.minutes`                                | number | 아니요    | 예상 분                                  |
| `status`                                      | string | 아니요    | `active` · `paused` · `ended`            |
| `quote`                                       | string | 아니요    | 들어올 때 고른 문구 (HM-10)              |
| `material` | object | 예 | 없으면 null, 있으면 id UUID와 page 정수 |
| `minutes`                                     | number | 아니요    | 서버가 잰 공부 분 (멈춘 시간 뺌)         |
| `pausedMinutes`                               | number | 아니요    |                                          |
| `checkIn`                                     | bool   | 아니요    | 예상 2배 지남 → '아직 공부 중이에요?'    |
| `autoStopped`                                 | bool   | 아니요    | 알림 뒤 10분 무응답 → 알림 시점에서 멈춤 |
| `excludeFromPace`                             | bool   | 아니요    | 속도 보정에서 뺀 기록                    |
| `recorded`                                    | bool   | 아니요    | 기록에 붙었는지                          |
| `todayMinutes` · `subjectMinutes`             | number | 아니요    | 오늘 총 · 과목 누적                      |
