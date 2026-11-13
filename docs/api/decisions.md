# 충돌 해소와 FE 변경표

사용자 지시에 따라 FE 계약을 기본으로 유지한다. 요구사항·와이어와 충돌하면 기존 BE 명세를 대조해 채택하고, BE에도 충돌/누락이 있으면 원문에 맞게 새 계약을 정의한다. 아래는 이번 구현 기준이며 FE 담당자의 수신·검토나 배포 완료를 의미하지 않는다. 변경 이유는 [원문 대조](source-audit.md)로 추적한다.

| 항목 | 이번 계약 | 출처/이유 | FE 변경 |
|---|---|---|---|
| 기본 주소/응답/인증 | /api, success/data/error, X-Anonymous-Id | FE 11/12 22시 결정 | 없음 |
| 장기 AI | PDF 작업 폴링/30초 FE timeout; 서버 예산 미확정 | 공통 Job(202) 취소 결정 존중 | 없음 |
| 진행 표시 | PDF 실제 current/progress, 나머지 대기 연출 | 숫자로 오인 방지 | 없음 |
| 빈 시험 | firstExam:null | 빈 사용자도 유효한 상태 | demo adapter와 헤더 null 분기 |
| 채점 실패 | 답안 보존 후 503 AI_FAILED, 동일 답안 재시도 | 답 저장 약속과 결과 정확성 | 저장된 answers 유지; quizId 전달 |
| 저장 채점 | 문항+답안 해시가 일치할 때만 사용 | 임의 70점 금지 | 저장 결과 없음을 실패 UI로 표시 |
| 미리보기 적용 | previewId 필수, base revision 검증 | 다중 탭/오래된 후보 적용 방지 | 아래 적용표대로 ID 보존·전달 |
| 시드 값 | AI 데모 패키지 기반의 별도 데이터 | 11/12 결정; DS/OS/NW 예시는 타입 예시 | [확정 값표](../demo-seed-handoff.md)대로 mock 값 교체 |
| P2/P4 | P2 먼저, P2 닫힌 후 P4 | policy-and-backend 11/12 결정 | 기존 순서 유지 |
| OCR | OCR 저품질만 quizless=true, 분석 실패만 수동 목차 | 팀 문서의 구별 | 없음 |
| 재분할 합계 | 실제 배정값 합산, 1025/1005 고정 금지 | 예시 수치 간 충돌 해소 | 없음 |
| tutor pages | {materialId,from,to}[] | 기존 adapter 채택 | 없음 |
| 단원 evidence | string[] 실제 근거 문장 | 기존 화면 채택; 좌표는 범위 제외 | 없음 |
| resplit days before/after | integer 분 | mock·예시·화면에 일치, 표의 array 오기 수정 | 없음 |
| 추가 계획 conflicts | integer 충돌 개수 | mock 구현과 일치 | 없음 |
| 여러 옵션 중 불가능 | enabled:false, 실행 거절 | 거짓 실행안 방지 | P2에서 disabled 표시/클릭 차단 |
| 수동 신규 단원 | PUT units의 id 생략 허용 | TOC 응답에 id 없음 | undefined 유지 가능 |
| 재시도 키 | 일반 요청은 선택적, relearn은 필수 Idempotency-Key | ERD 직접 회독 근거 보존 | relearn에 키 전달, 자동 재시도에는 같은 키 |
| 미지원 기능 | AI 회고 제외; 과목 추가/rebuild는 요구사항대로 지원 | PDF pp.13,17 및 사용자 충돌 해결 지시 | 신규 과목은 draft 온보딩 후 승인, rebuild는 비교 후 적용으로 구현 |

## 미리보기 식별과 수명

모든 후보 응답에 `previewId:string`을 추가한다. 이는 plan_proposals.id이며 소유자, 작업 종류, 대상 ID, 입력, 기준 plans.revision(계획 없으면 입력 구조 버전; 일정/자료/학습 기록 등 의존 데이터 버전도 포함)을 저장한다. 시간에 따른 자동 만료는 이번 계약에 두지 않는다. 이전30분 제한은 철회한다. 사용자 날짜 또는 후보가 참조한 과목·자료·일정·학습 기록·계획 버전이 바뀌면 stale로 판정한다. 소유권 불일치는 404 NO_PREVIEW, 없음은400 NO_PREVIEW, 기준 날짜/버전 불일치는409 PREVIEW_STALE, ID 누락은409 PREVIEW_REQUIRED. 성공 적용은 후보를 applied로 기록하고 같은 후보 재적용은 최초 응답을 반환한다. 취소 후보는 재적용 불가.

후보 생성 자체가 기존 계획 revision을 변경하지 않는다. 승인 성공은 revision을 한 번 증가시킨다. 서로 다른 후보 동시 승인은 첫 성공만 유효하며 나머지는 PREVIEW_STALE. 미리보기 단계에서 실제 일정/기록/목표를 변경하지 않는다.

| 후보를 받는 요청 | 적용 요청과 전달 방법 |
|---|---|
| GET /plan/draft | POST /plan/approve body {previewId} |
| POST/GET /plan/resplit | POST /plan/resplit/apply body {previewId} |
| GET /plan/replan/options | POST /plan/replan body {key,previewId} |
| PATCH /subjects/:id/preview | PATCH /subjects/:id body {previewId} |
| DELETE /subjects/:id/preview | DELETE /subjects/:id query previewId |
| POST /plan/additions/preview | POST /plan/additions body {previewId} |
| POST /plan/today/preview | POST /plan/today body {action,previewId} |
| POST /schedules/voice | POST /schedules/voice/apply body {previewId} |
| PATCH /schedules/:id/preview | PATCH /schedules/:id body {previewId} |
| DELETE /schedules/:id/preview | DELETE /schedules/:id query previewId |
| PUT /units 또는 PATCH /materials/:id/units의 applied:false 응답 | 동일 경로 body {previewId} |
| POST /plan/rebuild | POST /plan/resplit/apply body {previewId} |
| DELETE /schedules/:id 또는 /subjects/:id의 빈 시간 응답 | POST /plan/empty-time body {option,previewId} |

경로는 모두 /api 접두사. toPlanDraft/toResplit/toReplan/toPlanPreview/toAdditionPreview/toVoicePreview/toEmptyTime가 previewId를 보존하도록 변경해야 한다. 속도 보정 pace는 subjectId+서버에 저장된 calibration ID/revision으로 제안을 검증하며 오래된 제안은 PREVIEW_STALE; FE가 서버 반환 calibrationId를 전달하는 보완도 함께 적용한다.

추가 FE 보완: `toTask`는 서버 pages/ranges를 보존, `toPaceAlert`는 calibrationId를 보존하고 postPace에서 전달한다. P2 toReplan은 enabled를 보존한다. `postRelearn`은 요청당 Idempotency-Key를 생성하고 재시도 시 유지한다. quiz adapter는 quizId를 보존한다.

## null 및 기본값의 구체적인 예

빈 사용자의 demo state: `firstExam:null,hasPlan:false,judgment:null`. 빈 books: `subjects:[],readiness:[]`. 활성 집중 세션 없음: data:null. 계획 없는 today: 404 NO_PLAN. quiz 결과 미제출:404 NO_RESULT. 제출 저장 후 채점 미완료:503 AI_FAILED. 자료 없음: 목록 files:[], file:null. 과목 목록의 미수집 시험/목표는 null이며 임의 시험일을 생성하지 않는다.

## 명세와 시연 데이터 분리

시연 데이터는 [확정 시드 값표](../demo-seed-handoff.md)로 관리한다. 11/12 합의와 달리 DS/OS/NW 및 집중 세션 없음으로 남았던 BE 구현을 AI/DB/OS·42분 집중 시드로 보정했다. 시험일·목표는 승인된 시연 설정이고 단원/페이지/근거는 실제 AI 패키지에서 읽는다. day9는 67%에서 부분 기록 후 74%, DB1은 risk, overdue는 다음날 replan이다. 새로운 값은 배포 후 새 세션 또는 명시적인 초기화에 적용한다.
