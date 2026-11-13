# ERD와 도메인 경계

현재 ERD는 구조 기준이며 노트에 적힌 모든 제약이 자동 생성 schema.py에 구현되어 있다는 뜻은 아니다. 아래는 구현 시 필요한 영속 규칙이며 이번 문서 브랜치에서 DB를 변경하지 않는다.

| API 도메인 | ERD 테이블 | 원자적 변경/소유권 경로 |
|---|---|---|
| 세션/온보딩 | users, subjects, onboarding_messages | user→subject; 목표/대화·일정 후보 구분 |
| 일정 | calendar_events, calendar_event_exceptions | event.user_id; event+예외+관련 승인 이동 함께 적용 |
| 자료 | materials, units, concepts | material→subject→user; 구조 retire·신규 생성·작업 무효화 함께 적용 |
| 계획 | plans, plan_change_requests, plan_proposals | plan/request.user_id; base revision 확인 후 후보 승인 |
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
- 사용자당 진행 집중 세션 하나: status IN ('running','paused') 부분 유일. 열린 session_segment도 세션당 하나.
- 채점 작업은 quiz_session당 현재 대표 작업 하나. 결과 적용은 quiz_session 잠금+attempt/mastery 반영 표식으로 중복 금지.
- calendar_event_exceptions(event_id,occurrence_date) 유일. 범위 시작<=끝, 시각 시작<끝, 점수0..100, 비음수 시간/개수.

원문 erd-constraints.sql이 없어 위 내용은 이번 명세의 제약 요구사항이다. 배포 시 마이그레이션으로 작성하고 SQLite뿐 아니라 PostgreSQL에서 검증해야 한다.

## 기존 ERD에 필요한 보완

- demo seed를 날짜에서 추측하지 않도록 users.demo_seed 또는 동등한 사용자 상태 컬럼 필요.
- previewId는 plan_proposals.id 사용. 생성/만료/적용 상태·원 응답을 plan_change_requests/changes에 저장할 스키마를 정한다. 만료는 created_at+30분으로 계산 가능.
- 사용자 입력 구조 revision이 없는 최초 계획은 자료/목표/일정 ID와 updated_at의 정규화 해시로 stale 여부를 검증한다.
- 퀴즈 답안 해시, 채점 소유 작업 lease 및 최종 응답 스냅샷을 저장할 필드가 필요. processed 표식만으로 외부 호출 동시 실행을 방지할 수 없으므로 조건부 작업 선점/만료 lease를 사용한다.
- 선택적 Idempotency-Key 지원에는 요청 key/hash/응답/status/만료를 저장할 전용 테이블 또는 동등한 내구 저장소가 필요. 로그에만 남겨 재시도를 구현하지 않는다.

API enum과 DB enum 변환은 [enums.md](enums.md). 필드가 단순히 이름만 다른 경우 DB를 FE 이름으로 바꾸지 않고 응답 조립에서 변환한다.
