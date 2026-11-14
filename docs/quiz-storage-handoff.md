# 퀴즈 영속 저장 인계 (Q-02)

Migration `0006_quiz_answer_storage`는 `0005_onboarding_state` 다음 revision이다. 기존 퀴즈·풀이·점수 이력은 수정하지 않고 답안 revision, 현재 답안 pointer, 채점 lease, 봉인·결과 저장 테이블을 추가한다. 실제 API/worker 연결과 집계 확정은 Q-03~05에서 수행한다.

최종 구조는 `quiz_answers`, `quiz_answer_heads`, `quiz_answer_receipts`, `quiz_grading_jobs`, `quiz_submission_states`의 5개 테이블이다. 최초 4개 설계에 receipt를 보완했다. 예를 들어 rev1의 A를 rev1로 동일 저장(no-op)한 뒤 B로 rev2가 되어도, 실제 수신했던 no-op 요청은 원래 rev1 응답을 재생해야 한다. 답안 이력만으로는 같은 원문의 미수신 stale 요청과 구분할 수 없으므로 item/request_revision/canonical answer hash에 대한 최초 response_answer_id를 저장한다. 과거 완료 답안에는 이 요청 이력이 없으므로 migration에서 receipt를 추정하지 않는다.

## 기존 기록 이관

기존 `quiz_attempts.answer`는 객관식에서 선택 번호가 아니라 선택한 보기 문구다. 현재 보기와 정확히 한 번 일치할 때만 인덱스를 복원한다. 중복 보기·변경된 보기는 `legacy_answer`에 원문을 보존하고 `input_recovered=false`, `answer_hash=null`로 남긴다. 단답·서술형 문자열과 답변 시각·날짜·확신도·점수는 그대로 보존한다. 원문 공백을 지우거나 채점용 단답 정규화를 저장 hash에 적용하지 않는다.

이미 완료한 세트는 봉인하고 당시 DB에 남아 있는 session/items/attempts/questions/sources/masteryHistory를 `legacy_snapshot`으로 보존한다. 이는 migration 시점의 보존 사실이며 실제 과거 API 응답이 아니다. 최초 제출 당시 전체 응답은 저장되어 있지 않으므로 `result_snapshot` 및 provenance/version/captured_at은 null이다. 입력 전체가 유일하게 복원된 경우에만 봉인 hash가 채워진다.

Q-05는 과거 완료 세트의 첫 복구 조회에서 현재 보존 사실로 생성한 결과를 `result_provenance=legacy_reconstructed`로 고정하는 호환 정책을 결정한다. 신규 제출의 실제 최초 결과는 `submitted`로 구별한다. 둘 다 `result_schema_version`과 `result_captured_at`을 함께 저장해야 한다. 과거 응답을 정확히 복원했다고 표시하거나 legacy 세트를 영구 503으로 처리하지 않는다.

확신도/점수/소유권이 잘못된 기존 데이터는 DDL 전에 검출하여 삭제 없이 중단한다. 데이터 이관은 온라인 Alembic 실행이 필요하며 `--sql` 출력은 DDL만 포함한다. Downgrade는 새 테이블의 답안·lease·스냅샷을 제거하므로 신규 데이터가 쌓인 환경에서는 백업과 코드 호환성을 먼저 확인해야 한다. 기존 풀이 행은 유지한다.

새 테이블의 cascade는 quiz item/quiz session 삭제에만 종속된다. 원본 question 및 근거를 삭제하는 역방향 cascade는 없다. 기존 demo reset 삭제 순서를 그대로 사용할 수 있다.

## 답안 저장 연산

`QuizAnswerRepository(connection).save(user_id, quiz_id, item_id, revision=0, choice=None, text=None, confidence=..., now=..., answered_date=...)`는 `AnswerSave(answer, created, is_current)`를 반환한다. 최초 저장 revision은 1이며 다음 변경은 반환된 revision으로 요청한다. `current`/`list_current`는 소유권을 검증한 현재 답안만 반환한다.

같은 입력은 revision/답변 시각을 늘리지 않는다. 실제 수신한 예전 요청 revision과 같은 입력을 다시 보내면 receipt의 최초 답안 revision을 반환하며 현재 head를 되돌리지 않는다. 이 경우 `is_current=false`일 수 있으므로 Q-03은 예전 답안으로 새 채점 작업을 접수하면 안 된다. 같은 원문이 과거 답안에 있다는 이유만으로 미수신 stale 요청을 재생하지 않는다. receipt와 answer/head 변경은 같은 transaction에 저장한다.

다른 입력의 오래된 revision은 `ANSWER_CONFLICT`다. 봉인 뒤에는 실제 수신 receipt의 재생만 허용하며 새 변경이나 수신한 적 없는 no-op 요청도 `QUIZ_FINALIZED`다. 봉인 이전에 수신했던 no-op receipt는 그 뒤 수정·봉인이 있어도 원래 응답을 재생한다.

현재 답안 교체는 user lock 아래에서 head의 기존 answer_id를 조건으로 갱신한다. 이전 revision의 미완료 작업은 같은 트랜잭션에서 취소한다. 과거 답안과 성공한 채점 결과는 이력으로 남긴다. 호출자가 바깥 transaction을 commit/rollback하며 저장소는 commit하지 않는다. Q-03은 답안 저장과 작업 접수를 한 transaction에서 수행한 뒤 외부 호출 전에 commit해야 한다.

## 채점 작업 연산

`GradingJobRepository.enqueue(user_id, quiz_id, item_id, revision=..., now=..., mode=None)`는 현재 답안에 대한 유일 작업을 반환한다. Q-04의 `0008_grading_execution_mode`는 `live/saved/backup` 실행 의도를 작업에 저장한다. 새 작업의 mode=None은 live이며, 기존 작업에 mode=None을 전달하면 저장된 mode를 그대로 유지한다. 명시한 mode가 기존과 다르면 내부 `ExecutionModeConflict`다. enqueue가 기존 saved/backup을 live로 승격하지 않는다. Q-03은 답안 저장과 enqueue를 같은 transaction에 두고, Q-05의 신규 saved bulk 제출은 enqueue(mode="saved")를 같은 transaction에 포함한다.

자동 worker는 `pending(now=..., limit=100)`으로 queued 또는 lease가 만료된 running만 찾고 저장 mode를 따른다. `available(user_id, now=..., limit=100)`은 기존 수동 재시도 후보 조회 호환을 위해 failed를 포함하므로 자동 polling에 사용하지 않는다. limit는 작업자 batch 크기이며 사용자 답안 개수 제한이 아니다.

`claim(user_id, job_id, owner=..., now=..., lease_for=timedelta(...))`는 선점한 작업(새 lease_token 포함) 또는 None을 반환한다. 호출자는 claim을 commit하고 DB transaction 밖에서 AI를 호출한 뒤 새 transaction에서 `complete` 또는 `fail`을 호출한다. 이 연산은 owner/token/revision/now를 모두 받고 현재 답안과 만료 여부까지 조건부 갱신하여 성공 여부 bool을 반환한다. 같은 worker 이름으로 재선점해도 token이 바뀌므로 이전 결과는 반영되지 않는다. `renew`도 같은 자격을 확인하며 기존 lease를 줄이지 않는다.

Q-04 자동 실행은 `claim(..., retry_failed=False, mode=None)`을 사용한다. 명시적 retry만 `retry_failed=True, mode=...`로 queued/failed/expired-running의 mode와 token을 원자적으로 바꿀 수 있다. 유효 running lease는 mode를 바꾸지 않고 None/처리 중을 반환한다. 이미 나간 이전 live 요청을 취소하거나 과금이 없었다고 보장하지 않는다. 오래된 token의 결과는 폐기하며 crash 후 saved/backup 재선점은 외부 요청을 시작하지 않는다. 서비스의 typed `MODE_CONFLICT`는 내부 결과이고 공개 HTTP 매핑은 Q-05에서 결정한다. low-level claim의 retry_failed 기본 True는 기존 명시적 claim 호출 호환용이며 자동 worker는 반드시 False를 전달한다.

실패는 답안 원문을 유지하고 result=null로 남긴다. 저장된 채점 결과는 `is_correct`, `essay_score`, `feedback`, `got_points`, `missing_points`, `grading_status`를 가진다. 서술형 점수는 정수 0..100이며 85점 정오 경계를 검증한다. 이 연산은 quiz_attempts/숙달도/취약 문항을 확정하지 않는다. JSON 결과를 지울 때 SQL NULL을 사용하며 JSON 리터럴 null을 채점 성공으로 취급하지 않는다.

Q-04는 내부 result에 선택적 `provenance`를 추가한다. 서버가 만든 `answer_hash`, `question_hash`, `source_hash`, `material_hash`, `model_version`, `prompt_version`, `schema_version`, `saved_result_id`만 허용한다. 입력 해시는 저장된 답안과 일치해야 하며, 저장 결과를 참조하면 같은 사용자·essay_grade·schema의 결과여야 한다. 해시는 소문자 SHA-256이고 NUL/surrogate/비정상 JSON 숫자는 저장 전에 거절한다. 이전 결과의 provenance는 null 또는 누락일 수 있다. **Q-05는 provenance가 없다는 사실을 현재 문항·근거·자료 버전과 일치한다는 증거로 간주하지 않아야 한다.** 신규 실행 결과는 worker가 만든 버전 해시를 재검사한 후에만 확정하고 legacy 복구 정책은 별도로 적용한다.

## 답안 집합 봉인

`QuizAnswerRepository.seal(user_id, quiz_id, expected_revisions={item_id: revision, ...}, now=...)`는 모든 현재 항목의 답안과 revision을 검증하고 정렬된 항목/revision/answerHash 집합을 봉인한다. 동일 집합 재호출은 최초 상태를 반환하며 다른 집합은 `QUIZ_FINALIZED`다. 봉인만으로 기존 quiz_sessions.status나 점수/이력을 확정하지 않는다.

`submission`은 소유권을 검증한 내부 저장 상태를 반환한다.

## 최초 결과 저장

`store_result_snapshot(user_id, quiz_id, expected_hash=..., snapshot=..., provenance=..., schema_version=..., now=...)`는 봉인 hash를 확인하고 최초 결과만 저장한다. 신규 세트는 봉인한 답안이 여전히 현재 revision이고 모든 해당 작업이 succeeded일 때만 저장할 수 있다. 미완료면 `AI_FAILED`이며 원문과 봉인은 유지된다. 기존 결과가 있으면 재계산한 다른 내용으로 덮어쓰지 않는다.

legacy의 `expected_hash=None`은 복원 불가능한 입력의 봉인에 한해 그대로 비교하며 provenance는 `legacy_reconstructed`만 허용한다. 신규 세트는 `submitted`만 허용한다. Q-05가 응답 스키마·집계·근거를 검증하고 동일 외부 transaction에서 실제 확정 및 이 스냅샷 저장을 수행해야 한다. 저장소는 AI 호출, API 노출, 집계 반영 또는 legacy 응답 생성 정책을 구현하지 않는다.
