# focus API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- 사용자당 running/paused 세션 최대 하나. 같은 task로 start 재호출은 기존 세션, 다른 task는 409 OTHER_ACTIVE. task는 현재 사용자·오늘·활성 계획 소속이어야 한다.
- active는 없으면 data:null. API status는 active/paused/ended; DB running→active, auto_stopped→ended+autoStopped:true.
- pause: active→paused, 반복 pause는 같은 상태 반환. resume: paused→active, active+checkIn은 확인 시점 갱신, 일반 active는 그대로. 종료 상태 resume은 409 ENDED. end는 active/paused→ended, 반복 end는 같은 종료 결과 반환.
- minutes/pausedMinutes는 서버 timestamp의 구간 합을 분 단위 내림한 정수. 열린 구간은 조회 시각까지. 음수 구간 금지. 세그먼트는 자정을 가로지를 수 있고 todayMinutes는 현지 날짜 경계로 자른다.
- 예상 시간 2배가 되면 checkIn:true. 알림 후 10분 무응답이면 알림 시점에 자동 종료, autoStopped/excludeFromPace=true. 조회가 늦어도 조회 시각까지 시간을 부풀리지 않는다.
- material은 자료가 없으면 null, 있으면 {id:string,page:integer}. 여러 자료면 task.ranges의 첫 범위를 기본으로 열며 다른 자료 선택은 ranges로 제공한다. recorded는 연결된 study_record 존재 여부. quote는 세션 생성 때 정해 고정한다.

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
| `material.id` · `page`                        | –      | 아니요    | [자료 열기]                              |
| `minutes`                                     | number | 아니요    | 서버가 잰 공부 분 (멈춘 시간 뺌)         |
| `pausedMinutes`                               | number | 아니요    |                                          |
| `checkIn`                                     | bool   | 아니요    | 예상 2배 지남 → '아직 공부 중이에요?'    |
| `autoStopped`                                 | bool   | 아니요    | 알림 뒤 10분 무응답 → 알림 시점에서 멈춤 |
| `excludeFromPace`                             | bool   | 아니요    | 속도 보정에서 뺀 기록                    |
| `recorded`                                    | bool   | 아니요    | 기록에 붙었는지                          |
| `todayMinutes` · `subjectMinutes`             | number | 아니요    | 오늘 총 · 과목 누적                      |
