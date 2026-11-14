# ERD와 도메인 경계

현재 ERD는 구조 기준이며 노트에 적힌 모든 제약이 자동 생성 schema.py에 구현되어 있다는 뜻은 아니다. 아래는 영속 규칙이며, 구현 완료한 항목은 해당 migration과 함께 표시한다.

| API 도메인 | ERD 테이블 | 원자적 변경/소유권 경로 |
|---|---|---|
| 세션/온보딩 | users, subjects, onboarding_messages | user→subject; 목표/대화·일정 후보 구분 |
| 데모 재시도 | demo_day_requests | user_id+idempotency_key 기본 키; next-day 성공 data와 상태 변경을 함께 commit |
| 일정 | calendar_events, calendar_event_exceptions | event.user_id; event+예외+관련 승인 이동 함께 적용 |
| 자료 | materials, units, concepts | material→subject→user; 구조 retire·신규 생성·작업 무효화 함께 적용 |
| 계획 | plans, plan_change_requests, plan_proposals, plan_proposal_states | plan/request.user_id; 후보 기준과 최초 결과를 저장하고 같은 요청의 선택을 CAS |
| 학습 배치 | study_blocks, study_tasks, study_task_ranges, daily_plan_totals | task→subject→user; block 지속 ID와 마감 분모 보존 |
| 집중/기록 | study_sessions, study_session_segments, study_records, study_record_ranges, speed_calibrations | session.user_id와 task 소유자 일치; record→task→subject→user |
| 점수/회독 | mastery_history, learning_passes | unit→material→subject→user; 근거 소비·점수 반영 중복 금지 |
| 퀴즈 | questions, question_sources, quiz_sessions, quiz_scopes, quiz_items, quiz_attempts, weak_questions | quiz.user_id와 question 단원 소유자 일치; 결과 확정·점수·취약 항목 원자적 반영 |
| 튜터 | tutor_messages, tutor_sources | message.user_id와 근거 자료 소유자 일치 |
| 알림 | user_notices | user_id+dedupe_key 유일, 하루 박스 한 번 |
| AI | ai_saved_results, ai_jobs, ai_usage_logs | user_id+입력해시+자료버전 검증; 실제 호출만 usage 기록 |
| 범위 밖 | reflection_reports | AI 회고 API는 이번 계약에 없음 |

## 필요한 DB 제약

- subjects(user_id,code), plans(user_id), notices(user_id,dedupe_key), quiz_attempts(quiz_item_id), 기록의 task/session 연결 중복 제한.
- 현재 units(material_id,sort_order), 현재 concepts(unit_id,code)와(unit_id,sort_order): retired_at IS NULL 부분 유일.
- study_tasks(plan_id,block_id): status IN ('planned','partial') 부분 유일. 과거 carried_over와 현재 배치는 공존 가능.
- 사용자당 진행 집중 세션 하나: status IN ('running','paused') 부분 유일. 열린 session_segment도 세션당 하나. `0004_focus_contract`의 두 부분 인덱스로 구현했다.
- 문항별 채점 작업은 quiz_item+답안 revision당 하나. 오래된 revision 결과는 반영하지 않는다. 최종 집계 작업은 quiz_session당 현재 대표 작업 하나. 결과 적용은 quiz_session 잠금+attempt/mastery 반영 표식으로 중복 금지.
- calendar_event_exceptions(event_id,occurrence_date) 유일. 범위 시작<=끝, 시각 시작<끝, 점수0..100, 비음수 시간/개수.

원문 erd-constraints.sql은 저장소에 없으며 위 명시 요구사항을 `0011_contract_invariants`로 구현했다. 현재 단원/개념과 활성 블록 부분 유일 인덱스, 페이지 범위/짝 nullable, 일정·배치 시간, 점수 범위, 비음수 분량을 `schema/contract-invariants.json`에서 생성한다. 원래의 task/session 연결·문항 시도 중복 제한과 0004/0006/0007 제약은 중복 생성하지 않는다. 교차 테이블 소유권·PDF 최대 페이지·실제 시험일/가용시간은 도메인 검증 책임이다.

0011은 모든 제약의 기존 불량행/중복행을 DDL 전에 검사한다. 발견하면 제약 이름과 함께 중단하며 행을 임의 삭제하거나 수정하지 않는다. retired 구조와 carried_over 과거 배치는 유일 제약에서 제외된다. 즉시 종료/멈춤의 0초 집중 구간은 허용하되 역전 시각은 거절한다. downgrade는 추가한 인덱스/CHECK만 제거하며 데이터를 유지한다. migration은 현재 앱의 변경 가능한 manifest 대신 `migrations/baselines/contract_invariants_0011.json`을 사용한다.

## 기존 ERD에 필요한 보완

- 집중 시작 당시 `expected_seconds`와 `quote`는 `0004_focus_contract`로 저장한다. 기존 데이터 처리와 downgrade는 [migration 문서](../db-migrations.md)를 따른다.

- demo seed 저장은 `0002_demo_seed`의 nullable `users.demo_seed`로 구현했다. 과거의 불명확한 선택 이력은 null로 유지한다.
- previewId는 기존 plan_proposals.id다. `0007_preview_storage`의 1:1 `plan_proposal_states`에 소유자/종류/대상, 정규화 입력·해시, 기준 날짜·계획 revision 또는 최초 구조 해시, 의존 버전, 후보 원 응답과 최초 적용 결과를 저장한다. 기존 `plan_proposals.changes`는 불변 변경안으로 유지한다. 사용자 잠금 아래 request 선택과 후보 상태 revision CAS를 함께 기록하며 시간 기반30분 만료는 없다. 실제 기준 비교·도메인 적용·plans.revision 증가는 SYS-05 책임이다.
- 사용자 입력 구조 revision이 없는 최초 계획은 도메인이 제공한 자료/목표/일정 ID·updated_at 구조를 canonical JSON으로 해시한다. 저장소는 도메인 SQL이나 배열의 의미를 추측하지 않는다. 입력/변경안/응답/버전 JSON의 중복키, 비문자열 키, NaN/Infinity, 순환, 잘못된 Unicode와 NUL은 DB 쓰기 전에 거절한다.
- 0007은 기존 request/proposal/대화·선택·시각을 바꾸지 않는다. 확인되는 legacy applied 사실만 따로 보존하며 알 수 없는 기준/최초 응답은 null이다. legacy 후보는 실행 불가, 이미 적용됐지만 원 응답이 없는 경우도 재실행 불가로 구분한다. 저장소 API와 SYS-05 인계는 [preview 저장 인계](../preview-storage-handoff.md)를 따른다.
- quiz_items.id를 API questionId로 사용하고 원본 questions.id와 구분한다. 문항별 답안 revision, 답안 해시, 채점 소유 작업 lease 및 최종 응답 스냅샷을 저장할 필드가 필요. processed 표식만으로 외부 호출 동시 실행을 방지할 수 없으므로 조건부 작업 선점/만료 lease를 사용한다.
- 선택적 Idempotency-Key 중 next-day는 `0003_demo_day_requests`의 전용 테이블로 구현했다. 경로/메서드는 테이블 용도로 고정하고 완료된 200 data만 저장한다. 자동 만료는 하지 않는다. 다른 도메인의 요청/status/만료 지원은 별도 구현이 필요하다.

API enum과 DB enum 변환은 [enums.md](enums.md). 필드가 단순히 이름만 다른 경우 DB를 FE 이름으로 바꾸지 않고 응답 조립에서 변환한다.

### 기록 요청 응답 재생 (R-04)

선택적 `Idempotency-Key`가 있는 POST records 및 PATCH task record의 성공 응답은
`record_requests`에 `(user_id, method, path, idempotency_key)`와 정규화한 입력 해시로 저장한다.
사용자 잠금 아래 기록·알림 변경과 응답 저장을 같은 트랜잭션으로 커밋한다.
같은 키의 재전송은 최초 응답을 그대로 반환하며 이후 기록 정정을 덮어쓰지 않는다.
본문이 다르면 `409 IDEMPOTENCY_CONFLICT`, 처리 중 사용자 잠금 경합은
`409 REQUEST_IN_PROGRESS`다. 실패한 요청은 응답을 저장하지 않아 같은 키로 재시도할 수 있다.
헤더가 없으면 기존 기록 동작을 유지한다. 현재 자동 만료는 없으며 명시적 데모 reset/seed는
해당 사용자의 기록과 함께 응답 재생 자료도 비운다. 다른 사용자의 키는 유지한다.

### 일별 마감 분모 (D-02)

`DailyTotalsService`는 마감 전 현재 배치의 task별 페이지 합을 계산하고, 날짜 마감 시
이월·제외 처리보다 먼저 `daily_plan_totals`에 확정한다. 새 학습과 보충은 같은 페이지여도
다른 task이면 각각 포함하며, 한 task 안 중복 범위는 자료별 합집합으로 계산한다.
페이지 없는 태스크의 예상 분은 별도 필드에 보관하고 페이지 분모에 섞지 않는다.
확정된 행의 분모와 확정시각은 반복 마감·계획 변경·과거 기록 정정으로 바꾸지 않는다.
완료 분자는 해당 날짜 배치의 최신 기록으로 다시 계산하므로 이월 후 원래 배치가
carried_over여도 기존 학습량이 사라지지 않는다. 미래 날짜의 완료량은0이다.
기존 확정 행은 당시 분모의 근거로 보존하며 현재 계획으로 덮어쓰지 않는다.
