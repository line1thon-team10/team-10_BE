# 퀴즈 영속 저장 인계 (Q-02)

Migration `0006_quiz_answer_storage`는 `0005_onboarding_state` 다음 revision이다. 기존 퀴즈·풀이·점수 이력은 수정하지 않고 답안 revision, 현재 답안 pointer, 채점 lease, 봉인·결과 저장 테이블을 추가한다. 실제 API/worker 연결과 집계 확정은 Q-03~05에서 수행한다.

## 기존 기록 이관

기존 `quiz_attempts.answer`는 객관식에서 선택 번호가 아니라 선택한 보기 문구다. 현재 보기와 정확히 한 번 일치할 때만 인덱스를 복원한다. 중복 보기·변경된 보기는 `legacy_answer`에 원문을 보존하고 `input_recovered=false`, `answer_hash=null`로 남긴다. 단답·서술형 문자열과 답변 시각·날짜·확신도·점수는 그대로 보존한다. 원문 공백을 지우거나 채점용 단답 정규화를 저장 hash에 적용하지 않는다.

이미 완료한 세트는 봉인하고 당시 DB에 남아 있는 session/items/attempts/questions/sources/masteryHistory를 `legacy_snapshot`으로 보존한다. 이는 migration 시점의 보존 사실이며 실제 과거 API 응답이 아니다. 최초 제출 당시 전체 응답은 저장되어 있지 않으므로 `result_snapshot` 및 provenance/version/captured_at은 null이다. 입력 전체가 유일하게 복원된 경우에만 봉인 hash가 채워진다.

Q-05는 과거 완료 세트의 첫 복구 조회에서 현재 보존 사실로 생성한 결과를 `result_provenance=legacy_reconstructed`로 고정하는 호환 정책을 결정한다. 신규 제출의 실제 최초 결과는 `submitted`로 구별한다. 둘 다 `result_schema_version`과 `result_captured_at`을 함께 저장해야 한다. 과거 응답을 정확히 복원했다고 표시하거나 legacy 세트를 영구 503으로 처리하지 않는다.

확신도/점수/소유권이 잘못된 기존 데이터는 DDL 전에 검출하여 삭제 없이 중단한다. 데이터 이관은 온라인 Alembic 실행이 필요하며 `--sql` 출력은 DDL만 포함한다. Downgrade는 새 테이블의 답안·lease·스냅샷을 제거하므로 신규 데이터가 쌓인 환경에서는 백업과 코드 호환성을 먼저 확인해야 한다. 기존 풀이 행은 유지한다.

새 테이블의 cascade는 quiz item/quiz session 삭제에만 종속된다. 원본 question 및 근거를 삭제하는 역방향 cascade는 없다. 기존 demo reset 삭제 순서를 그대로 사용할 수 있다.

## 답안 저장 연산

`QuizAnswerRepository(connection).save(user_id, quiz_id, item_id, revision=0, choice=None, text=None, confidence=..., now=..., answered_date=...)`는 `AnswerSave(answer, created, is_current)`를 반환한다. 최초 저장 revision은 1이며 다음 변경은 반환된 revision으로 요청한다. `current`/`list_current`는 소유권을 검증한 현재 답안만 반환한다.

같은 입력은 revision/답변 시각을 늘리지 않는다. 예전 요청 revision과 같은 입력을 다시 보내면 그 요청이 처음 만든 답안 revision을 반환하며 현재 head를 되돌리지 않는다. 이 경우 `is_current=false`일 수 있으므로 Q-03은 예전 답안으로 새 채점 작업을 접수하면 안 된다. 다른 입력의 오래된 revision은 `ANSWER_CONFLICT`, 봉인 뒤 변경은 `QUIZ_FINALIZED`다. 봉인 뒤에도 이미 저장된 동일 요청의 응답 재생은 가능하다.

현재 답안 교체는 user lock 아래에서 head의 기존 answer_id를 조건으로 갱신한다. 이전 revision의 미완료 작업은 같은 트랜잭션에서 취소한다. 과거 답안과 성공한 채점 결과는 이력으로 남긴다. 호출자가 바깥 transaction을 commit/rollback하며 저장소는 commit하지 않는다. Q-03은 답안 저장과 작업 접수를 한 transaction에서 수행한 뒤 외부 호출 전에 commit해야 한다.

## 채점 작업 연산

`GradingJobRepository.enqueue(user_id, quiz_id, item_id, revision=..., now=...)`는 현재 답안에 대한 유일 작업을 반환한다. `available(user_id, now=..., limit=100)`은 queued/failed 또는 lease가 만료된 현재 revision 작업을 찾는다. limit는 작업자 batch 크기이며 사용자 답안 개수 제한이 아니다.

`claim(user_id, job_id, owner=..., now=..., lease_for=timedelta(...))`는 선점한 작업(새 lease_token 포함) 또는 None을 반환한다. 호출자는 claim을 commit하고 DB transaction 밖에서 AI를 호출한 뒤 새 transaction에서 `complete` 또는 `fail`을 호출한다. 이 연산은 owner/token/revision/now를 모두 받고 현재 답안과 만료 여부까지 조건부 갱신하여 성공 여부 bool을 반환한다. 같은 worker 이름으로 재선점해도 token이 바뀌므로 이전 결과는 반영되지 않는다. `renew`도 같은 자격을 확인하며 기존 lease를 줄이지 않는다.

실패는 답안 원문을 유지하고 result=null로 남긴다. 저장된 채점 결과는 `is_correct`, `essay_score`, `feedback`, `got_points`, `missing_points`, `grading_status`를 가진다. 서술형 점수는 정수 0..100이며 85점 정오 경계를 검증한다. 이 연산은 quiz_attempts/숙달도/취약 문항을 확정하지 않는다. JSON 결과를 지울 때 SQL NULL을 사용하며 JSON 리터럴 null을 채점 성공으로 취급하지 않는다.
