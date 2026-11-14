# O-01 온보딩 상태 인계

구현 범위는 목표·확인 사실·대화의 영속 상태와 `GET /api/onboarding`이다. 실제 자연어 해석과 `POST /api/onboarding/message`는 O-03, 일정 CRUD·주간 가용시간은 O-02 후속이다. 이 경로들을 성공 응답으로 대신하는 stub은 추가하지 않았다.

## FE 조회와 화면 상태

- 초기 온보딩은 `GET /api/onboarding`, 추가 draft 과목은 `GET /api/onboarding?subjectId=…`를 호출한다. 모든 호출에 `X-Anonymous-Id`가 필요하며 성공은 공통 `{success:true,data:…}` 형식이다.
- `messages`, `chips`, `subjects`, `schedules`는 항상 배열이다. 빈 계정은 모두 빈 배열이고 `done=false`다. 서버가 최초 AI 대화를 만들어 저장하거나 AI 해석을 실행하지 않는다.
- 카드의 `examDate`, `goal`, `goalTotal`, `goalEtc`, `target` 키는 값이 없으면 null이다. `confirmedFields`의 `examDate/goal/goalTotal/goalEtc`는 항상 boolean 4개를 반환한다.
- `status=pending`은 수집된 값과 확인 사실이 없는 상태다. 일부 값이나 확인 사실이 있으면 `asking`, 네 확인값과 유효한 시험일·목표 학점이 모두 있으면 `confirmed`다. `done`은 과목이 하나 이상이고 모든 카드가 `confirmed`일 때만 true다. `target`은 nullable이며 없다는 이유만으로 다음 단계를 막지 않는다.
- 기타 정보나 시험 총 목표를 명시적으로 ‘없음’이라고 확인하면 값은 null이고 해당 확인값은 true일 수 있다. FE는 null 검사로 확인 여부나 `done`을 다시 계산하지 않는다. 시험일과 목표 학점은 실제 값 없이 확인할 수 없다.
- 이 카드의 `pending/asking/confirmed`는 목표 수집 상태다. 과목 관리의 `draft/active/archived` 또는 사용자의 계획 승인 상태와 다르다. `done=true`여도 과목은 draft이며 계획이 생성·승인되지 않는다.

예를 들어 목표 정보가 모두 확인되었지만 선택 정보가 없는 카드는 다음 상태를 갖는다.

```json
{
  "examDate": "2026-12-01",
  "goal": "A+",
  "goalTotal": null,
  "goalEtc": null,
  "target": null,
  "status": "confirmed",
  "confirmedFields": {"examDate": true, "goal": true, "goalTotal": true, "goalEtc": true}
}
```

## 컨텍스트와 재접속

초기 컨텍스트는 최초 승인 전 draft 과목, 승인 후 active 과목만 반환한다. 승인 후 새로 만든 draft가 기존 초기 완료 상태에 섞이지 않는다. `subjectId` 컨텍스트는 해당 사용자 소유 draft 카드와 대화만 반환하며, 다른 draft의 대화·칩을 섞지 않는다. 타인·없는·보관 과목은 `404 SUBJECT_NOT_FOUND`, active 과목을 draft 컨텍스트로 조회하면 `409 ONBOARDING_COMPLETED`다.

일정 조회는 초기 컨텍스트에서 사용자 공통 일정과 대상 과목의 일정, 과목 컨텍스트에서는 해당 과목에 연결된 일정만 포함한다. 삭제된 일정과 시험 이벤트는 제외한다. 일회성 일정은 date, 주간 반복은 중복 없는 days를 반환한다. 이 조회가 승인된 일정·과목·계획을 바꾸지는 않는다.

메시지는 사용자와 초기/과목 컨텍스트별로 저장 순서를 보존한다. 최종 메시지가 AI이면 그 메시지의 chips를, 사용자이면 빈 chips를 반환한다. 재접속은 저장한 대화와 확인 상태를 그대로 복구한다.

## 저장 구조와 레거시 처리

`0005_onboarding_state`는 `0004_focus_contract` 다음 revision이다. subjects에 nullable 확인 시각 4개를, onboarding_messages에 nullable subject_id FK와 sort_order를 추가한다. 확인 시각의 존재가 응답 boolean의 근거다. 기존 `goal_confirmed_at`은 네 항목 각각의 확인 증거가 아니므로 새 컬럼은 모두 null로 시작한다.

특히 기존 시드는 목표와 `goal_confirmed_at`을 저장하지만 선택 정보의 명시적 없음 응답을 기록하지 않았다. seed/legacy의 null 값이나 승인 여부를 근거로 확인 사실을 추정하지 않는다. 따라서 해당 계정의 초기 조회는 카드 `asking`, `done=false`일 수 있지만 기존 `onboarding_status=completed`, active 과목, 계획은 그대로다. `done`은 S1 입력 준비 상태이며 기존 계획 승인 여부를 대체하지 않는다.

기존 change_request_id 연결 대화는 행과 연결을 보존한다. 구조가 확정되지 않은 request payload에서 subjectId를 추측해 새 컨텍스트로 옮기지 않으므로 기존 제안 대화는 초기 조회에 섞이지 않는다. 그러한 레거시 대화를 새 과목 컨텍스트에 복구하는 이관은 이 변경에 포함되지 않는다. 기존 초기 대화의 sort_order=null은 created_at/ID 순으로 새 메시지보다 먼저 반환한다.

기존 revision은 변경하지 않는다. 이 revision의 downgrade는 기존 과목·대화 행을 보존하지만 새 확인/컨텍스트/순서 메타데이터를 잃는다. 사용자 seed/reset은 기존 삭제 순서대로 대화를 먼저 지우므로 새 subject FK와 충돌하지 않는다.

## O-03 연결 경계

`OnboardingService.save_turn(subject_id=…, goals=[GoalUpdate(…)], messages=[ConversationMessage(…)])`는 이미 해석한 값을 저장한다. API 응답 문장을 임의 생성하거나 외부 AI를 호출하지 않는다. 호출자가 트랜잭션 commit/rollback을 책임진다.

저장 시 사용자 행 잠금을 얻은 뒤 승인 상태와 대상 소유권을 다시 확인한다. 잠금 경합은 `409 REQUEST_IN_PROGRESS`다. 전체 입력 검증 뒤 목표와 대화를 함께 저장하며 응답 생성 실패도 호출자 트랜잭션에서 롤백해야 한다. 이미 승인된 초기 컨텍스트에는 쓸 수 없고, 별도 draft 컨텍스트만 수정할 수 있다.

`GoalUpdate.values`는 다음 ERD 필드만 받는다. `confirmed_fields`는 응답과 동일한 4개 이름의 명시적 boolean 부분 patch다. 값 변경은 해당 그룹의 확인을 해제하며, 같은 저장에서 명시적으로 다시 확인한 그룹만 확정된다. 값이 같고 확인 상태도 같으면 확인 시각을 다시 쓰지 않는다.

| 응답/확인 그룹 | 저장 필드 |
|---|---|
| examDate | exam_date |
| goal | target_grade; raw_goal/target_score 변경도 이 확인을 해제 |
| goalTotal | passing_criteria, grade_criteria를 순서대로 줄바꿈 표시 |
| goalEtc | grading_weights, emphasized_content, other_exam_info를 순서대로 줄바꿈 표시 |

시험일은 유효한 date, 목표 텍스트는 string/null, 점수는 bool/문자열을 제외한 유한 0..100 숫자다. 빈 선택 텍스트는 null로 정규화한다. 외부 호출은 잠금 밖에서 수행해야 하며, 해석 시작 당시 입력 버전의 검증과 공개 요청 재시도 처리는 O-03 및 공통 후속에서 연결해야 한다. 이 저장 메서드만으로 오래된 AI 해석 결과의 최신성이나 Idempotency-Key 처리가 완성되었다고 간주하지 않는다.

## 검증

`tests/test_onboarding_migration.py`는 SQLite/PostgreSQL에서 기존 목표·대화·제안 연결 보존, 반복 upgrade, downgrade/re-upgrade, 모델 일치와 FK 삭제 경계를 확인한다. `tests/test_onboarding_state.py`는 빈 값·명시적 없음·숫자 경계·부분 변경·재접속·컨텍스트 소유권·잠금 경합·롤백·기존 계획 보존을 확인한다. `tests/test_onboarding_api.py`는 실제 등록 경로, 인증·쿼리 오류, nullable 응답, 재조회 불변성, 레거시 시드, seed 초기화의 FK 호환성을 확인한다. AI 실호출이나 FE 화면 연결 검증은 수행하지 않았다.
