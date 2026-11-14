# 퀴즈 영속 저장 인계 (Q-02)

Migration `0006_quiz_answer_storage`는 `0005_onboarding_state` 다음 revision이다. 기존 퀴즈·풀이·점수 이력은 수정하지 않고 답안 revision, 현재 답안 pointer, 채점 lease, 봉인·결과 저장 테이블을 추가한다. 실제 API/worker 연결과 집계 확정은 Q-03~05에서 수행한다.

## 기존 기록 이관

기존 `quiz_attempts.answer`는 객관식에서 선택 번호가 아니라 선택한 보기 문구다. 현재 보기와 정확히 한 번 일치할 때만 인덱스를 복원한다. 중복 보기·변경된 보기는 `legacy_answer`에 원문을 보존하고 `input_recovered=false`, `answer_hash=null`로 남긴다. 단답·서술형 문자열과 답변 시각·날짜·확신도·점수는 그대로 보존한다. 원문 공백을 지우거나 채점용 단답 정규화를 저장 hash에 적용하지 않는다.

이미 완료한 세트는 봉인하고 당시 DB에 남아 있는 session/items/attempts/questions/sources/masteryHistory를 `legacy_snapshot`으로 보존한다. 이는 migration 시점의 보존 사실이며 실제 과거 API 응답이 아니다. 최초 제출 당시 전체 응답은 저장되어 있지 않으므로 `result_snapshot` 및 provenance/version/captured_at은 null이다. 입력 전체가 유일하게 복원된 경우에만 봉인 hash가 채워진다.

Q-05는 과거 완료 세트의 첫 복구 조회에서 현재 보존 사실로 생성한 결과를 `result_provenance=legacy_reconstructed`로 고정하는 호환 정책을 결정한다. 신규 제출의 실제 최초 결과는 `submitted`로 구별한다. 둘 다 `result_schema_version`과 `result_captured_at`을 함께 저장해야 한다. 과거 응답을 정확히 복원했다고 표시하거나 legacy 세트를 영구 503으로 처리하지 않는다.

확신도/점수/소유권이 잘못된 기존 데이터는 DDL 전에 검출하여 삭제 없이 중단한다. 데이터 이관은 온라인 Alembic 실행이 필요하며 `--sql` 출력은 DDL만 포함한다. Downgrade는 새 테이블의 답안·lease·스냅샷을 제거하므로 신규 데이터가 쌓인 환경에서는 백업과 코드 호환성을 먼저 확인해야 한다. 기존 풀이 행은 유지한다.

새 테이블의 cascade는 quiz item/quiz session 삭제에만 종속된다. 원본 question 및 근거를 삭제하는 역방향 cascade는 없다. 기존 demo reset 삭제 순서를 그대로 사용할 수 있다.
