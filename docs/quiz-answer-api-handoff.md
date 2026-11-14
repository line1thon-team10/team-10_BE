# Q-03 문항 답안 저장·복구 인계

기준 HEAD는 `9d1ddd7a40e37f74823950038581b18e1134fc58`다. Q-02 저장소 위에 문항별 PUT과 GET 복구를 연결했다. 기존 bulk submit·문항 선택·DB 스키마·worker는 변경하지 않았다. Q-03 단독 운영 배포는 금지하며 `deployment_requires=[Q-05]`다. 개발 통합 가능 여부와 운영 배포 가능 여부를 구분한다.

## FE 연결

`GET /api/quiz/{subjectId}?quizId={quizId}` 또는 기존 세트 ID 경로의 `questions[].id`를 PUT의 `questionId`에 사용한다. 이 값은 `quiz_items.id`이며 원본 `questions.id`가 아니다. 같은 원본 문제도 다른 세트에서는 다른 항목이다.

```http
PUT /api/quiz/{quizId}/answers/{questionId}
X-Anonymous-Id: <현재 익명 세션>
Content-Type: application/json

{"choice":0,"text":null,"confidence":"sure","revision":0}
```

성공은 `200 {"success":true,"data":{"quizId":"…","questionId":"…","saved":true,"revision":1}}`다. 최초 저장은 revision 0, 이후 수정은 서버가 반환한 revision을 보낸다. 응답에는 정오·점수·해설·근거·채점 상태가 없다. 저장 완료는 채점 완료를 뜻하지 않는다.

- `choice`는 0 기반 정수이며 현재 보기 범위 안이어야 한다. bool·float·숫자 문자열은 거절한다. OX도 choice다.
- short/essay는 `text` 문자열을 명시한다. 빈 문자열·공백·줄바꿈과 유효한 Unicode를 원문 그대로 저장하며 임의 길이 제한을 추가하지 않았다. NUL과 단독 surrogate는 UTF-8/hash·PostgreSQL 저장 경계가 일치하도록 HTTP와 저장소 모두 쓰기 전에422로 거절한다. 사용하지 않는 choice/text는 null 또는 생략한다. 둘 다 비어 있거나 두 유형을 혼합하면 422다.
- confidence는 sure/unsure 필수다. 누락은 기존 퀴즈 계약대로 `400 CONFIDENCE_REQUIRED`, null/다른 값은 `422 INVALID_INPUT`이다.
- revision은 필수 정수 0 이상이다. 추가 필드는 `422 INVALID_INPUT`이며 savedGrading 같은 채점 제어 입력도 받지 않는다.
- GET의 `savedAnswers`는 현재 세트의 최신 저장 입력만 질문 순서로 반환한다. 각 원소는 `{questionId,choice,text,confidence,revision}`이며 nullable choice/text 키도 항상 포함한다. 미답안은 `[]`다.

기존 FE의 마지막 bulk submit만으로는 문항 이동 시 저장되지 않는다. FE는 [다음] 또는 답 변경 시 PUT을 연결하고 재조회 시 savedAnswers로 입력을 복구해야 한다. 네트워크 재시도는 같은 본문과 요청 revision을 유지한다. 이전 응답을 재생한 revision이 현재 head보다 작을 수 있으므로 늦게 도착한 과거 응답으로 최신 로컬 revision을 덮어쓰지 않는다. 충돌 시 동일 quizId로 다시 조회한다.

## 원자성·재시도·현재 조건

한 요청은 사용자 행 잠금 → 최신 사용자·소유 세트·소유 자료 재조회 → 답안/영수증/head 저장 → 새 revision의 job 등록 → 요청 transaction commit 순서다. 잠금 경합은 `409 REQUEST_IN_PROGRESS`다. UTC 판단 시각은 한 번만 잡고 최신 demo_date 또는 사용자 timezone으로 answered_date를 계산한다. enqueue 전후 장애는 새 답안·영수증·head 및 이전 job 취소까지 전부 rollback한다.

`AnswerSave.created=true`일 때만 `GradingJobRepository.enqueue(..., revision=..., now=...)`를 호출한다. mode 인수는 생략하며 Q-04의 저장 mode를 변경하지 않는다. API는 외부 AI를 호출하거나 실패 점수 0을 만들지 않는다. attempts·숙달도·취약 문항 확정은 Q-05 소유다.

실제로 수신했던 `(item, request_revision, 입력 hash)` 영수증은 최초 응답을 재생한다. 현재 답보다 오래된 응답도 head를 되돌리거나 새 job을 만들지 않는다. 열린 세트의 현재 revision에 같은 원문을 보내는 no-op은 revision·answered_at을 늘리지 않고 해당 요청 영수증만 저장한다. 미수신 stale revision이나 미래 revision은 `409 ANSWER_CONFLICT`다.

기본 입력과 현재 소유권 검증 뒤, 실제 영수증의 answer item/hash가 일치해야 재생한다. 원래 성공 후 문항 유형이나 보기 개수가 바뀌어도 해당 영수증은 재생하며 현재 유형·보기 검사는 미수신 요청에만 적용한다. 손상된 영수증은409로 거절한다. concept가 있으면 question과 같은 unit이어야 하며, 다른 소유 단원 또는 타인 단원의 concept 연결은 PUT·GET·재생 모두404다.

새 요청은 ready/in_progress에서만 받는다. generating/failed는 `404 NO_QUIZ`, submitted/carried_over/finalized_at 또는 Q-02 봉인이 있으면 `409 QUIZ_FINALIZED`다. migration 이후 기존 submit/seed가 만든 sidecar 없는 완료 세트도 session 상태로 차단한다. 봉인·종료 뒤에는 실제 수신 영수증만 200이며, 같은 원문이라도 수신한 적 없는 요청 revision은 거절한다.

daily 신규 요청은 D-01의 현재 날짜별 PDF 합집합 진행률로 70%를 다시 판정한다. 완료 범위 정정, quiz_enabled 해제, 시험 범위 제외, 배치 제외, 날짜 이동 후 닫히면 `403 QUIZ_LOCKED`다. 이미 수신한 영수증은 상태·열림 검사보다 우선하여 재생하지만, 잠긴 상태에서 미수신 no-op 영수증을 새로 만들지는 않는다. practice/extra에는 기존 GET과 같이 daily 진행률 조건을 적용하지 않는다. GET의 기존 daily 잠금 규칙은 유지한다.

## 읽기·legacy 경계

GET 복구는 Q-02 `list_current`의 세트·자료 소유권 검사를 거친다. 저장 답이 없어도 타인 자료가 섞인 세트는 반환하지 않는다. `input_recovered=false`는 savedAnswers에서 제외한다. legacy_answer나 기존 quiz_attempts.answer에서 보기 인덱스를 추측하지 않으며, 복원된 입력이 없는 완료 세트는 빈 배열을 유지한다. 원본 풀이·봉인·점수는 수정하지 않는다.

Q-02의 제출 저장 연산은 현재 `QuizAnswerRepository` 안의 seal/submission/store_result_snapshot에 있다. 기준 트리에는 별도 `repositories/quiz_submissions.py`가 없다.

## Q-05 배포 선행 조건: 실제 legacy submit 경합

Q-02 seal과 PUT의 양방향 경합은 SQLite·PostgreSQL에서 검증했다. seal이 먼저 잠그면 PUT은 REQUEST_IN_PROGRESS이며 seal commit 뒤에는 QUIZ_FINALIZED다. PUT 수정이 먼저 commit되면 오래된 expected_revisions를 가진 seal은 ANSWER_CONFLICT이고 새 revision을 확인한 뒤에만 봉인된다.

기존 HTTP `POST /api/quiz/{key}/submit`은 이 봉인 경로를 아직 사용하지 않는다. 현재 코드는 user lock 없이 답을 읽고 채점하며 quiz_attempts와 session만 확정한다. 이를 Q-03에서 잠금 하나로 감싸면 외부 AI 호출 중 사용자 잠금을 유지하게 되므로 해당 변경은 하지 않았다. Q-05가 **답안 저장·봉인 commit → transaction 밖 채점 → 잠금/버전 재검사 후 확정**으로 교체하고 실제 HTTP 경합을 통과해야 한다.

수정 후 코드 HEAD `5c8411edb0902a3fc1a1fecb984a72bb07ee9ffe`에서 실제 PostgreSQL로 재현했다.

1. ready practice 세트를 실제 HTTP submit하고 서술형 채점 진입 위치에서 대기시킨다.
2. 별도 HTTP PUT으로 같은 서술형 항목에 다른 text, revision 0을 보낸다.
3. PUT이 **200**이고, 대기를 해제하면 submit도 **200**이다.
4. `quiz_answers.text='제출 도중 바뀐 답안'`인데 확정 `quiz_attempts.answer=''`이며 submission sidecar는 없다.

기대하는 PUT 409 검사는 **1 failed**로 남았다. 통과/xfail로 숨기지 않았다. 임시 재현 파일은 `/private/tmp/team10-quiz-answer-legacy-race/test_legacy_quiz_submit_race.py`, 실제 로그는 `/private/tmp/team10-quiz-answer-legacy-race.log`다. Q-05에서 이 순서와 반대 순서, 같은 입력 재시도, AI 실패/재시도까지 실제 HTTP로 닫아야 한다. Q-03의 단독 통과 검사와 이 배포 차단 실패를 합쳐 성공으로 보고하지 않는다.

## 검증

공유 `.venv`를 변경하지 않고 로컬 테스트 DB의 fixture별 격리 스키마를 사용했다. 아래 환경 변수는 로컬 테스트 전용이며 운영 DB를 사용하지 않는다.

```sh
export TEST_POSTGRES_URL=postgresql+psycopg://postgres:team10-local-test@127.0.0.1:54511/team10_test
.venv/bin/python -m pytest -q tests/test_quiz_answer_api.py tests/test_quiz_answer_api_concurrency.py tests/test_quiz_answer_api_recovery.py --tb=short
.venv/bin/python -m pytest -q tests/test_quiz_answer_api.py tests/test_quiz_answer_api_concurrency.py tests/test_quiz_answer_api_recovery.py tests/test_quiz_storage.py tests/test_quiz_identifiers.py tests/test_demo_quiz_flow.py tests/test_contract_inventory.py tests/contract_cases --tb=short
.venv/bin/python scripts/check_api_spec.py --runtime
```

Q-03 전용 테스트는 SQLite·PostgreSQL 합계 186 passed다. 입력/소유권, 0번 보기·빈 서술형, 과거 영수증 재생, 실제 user lock/revision 경합, enqueue 전후 rollback, 새 요청·연결의 입력 복구, late worker 차단, legacy와 정답 비노출을 포함한다. QA-01 runtime inventory는 신규 PUT을 등록된 계약으로 인식하며 uncontracted/duplicates는 비어 있다. 전체 필수 74개 중 28개 등록·46개 미등록으로, 경로 등록 검사 통과가 전체 API 구현 완료를 뜻하지 않는다.

기존 Q-02 저장소·Q-01 식별·demo와 QA-01 HTTP 하네스를 함께 실행한 두 번째 명령은 **569 passed, 5 warnings, 199.44초**였다. 경고는 기존 SWIG 타입의 `__module__` deprecation이며 로그는 `/private/tmp/team10-quiz-answer-regression.log`에 남겼다. 위 legacy HTTP submit 경합의 별도 1 failed는 이 통과 집계에 포함되지 않는다.

원자 구현 커밋은 `c0aa7d9`(PUT와 관련 테스트), `5c8411e`(GET 복구와 관련 테스트)다. 이 문서는 별도 인계 커밋으로 분리한다.

## 독립 검토 보완

`fffdfd9`의 독립 테스트는20 failed/2 passed로 문자열 저장 경계, concept 소속, metadata 변경 후 receipt 재생의 세 결함을 검출했다. 각각 `7d1f49d`, `83acd27`, `4022fa7`로 수정했다. BE의 검증 누락/처리 순서 문제였으며 FE가 잘못된 값을 보냈다는 이유로500을 허용하지 않는다. FE는 유효한 기존 원문·요청 revision을 그대로 재전송한다.

SQLite·PostgreSQL 영향 검증은 문자열+API166 passed(52.44초), concept+GET44 passed(14.40초), receipt+API+저장소334 passed(102.72초)다. 세 실행에는 중복 사례가 있으므로 합쳐 고유 테스트 수로 보고하지 않는다. 로그는 각각 `/private/tmp/team10-q03-text-fix.log`, `team10-q03-concept-fix.log`, `team10-q03-receipt-fix.log`다. 고정 코드 SHA `4022fa7035589405733ba016bab8df09347001f1`을 원래 독립 재현으로 다시 검사 중이며, 검증 전 승인으로 표시하지 않는다.
