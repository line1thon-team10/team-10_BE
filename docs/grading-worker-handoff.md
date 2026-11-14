# 재시작 가능한 채점 실행기 인계 (Q-04)

Q-04는 저장한 답안 revision별 채점 작업을 실행하고 검증된 결과만 저장한다. 문항별 답안 API 연결은 Q-03, 세트 봉인·`quiz_attempts`·숙달도·취약 문항·최초 결과 확정은 Q-05다. 기존 quiz router/submit 경로는 이번 변경에서 수정하지 않았다. 따라서 worker 서비스가 추가됐다는 사실만으로 기존 제출 API가 새 내구성 경로에 연결됐다고 볼 수 없다.

## 트랜잭션과 결과의 경계

`GradingService(engine, owner=...)`는 caller의 Connection을 소유하거나 닫지 않는다. 다음 세 구간으로 실행한다.

1. 사용자/퀴즈 잠금 아래에서 현재 답안과 소유권을 확인하고 lease를 원자 선점한다. 답안·문항·근거·자료·단원/개념 범위를 immutable JSON으로 캡처하고 commit한다.
2. 연결과 잠금을 모두 해제한 뒤 결정적 채점 또는 격리된 AI gateway 실행을 수행한다. 메모리 snapshot은 외부 코드가 수정할 수 없으며, 프로세스가 사라지면 만료된 lease를 재선점해 DB에서 다시 캡처한다.
3. 새 transaction에서 현재 답안 revision, lease token/owner/만료, 실행 mode, 전체 입력 snapshot을 재검사한다. 일치할 때만 complete/fail한다. 답안 변경·취소·새 token의 늦은 결과는 반영하지 않는다. 문항/근거/자료 변경은 `GRADING_INPUT_CHANGED` 실패로 남긴다.

소유권은 quiz → subject/user 및 question → unit → material/subject 체인과 모든 source material에 적용한다. 개념이 있으면 같은 문항 단원에 속해야 한다. 비교용 시각은 SQLite naive UTC와 PostgreSQL timezone 표현을 모두 UTC ISO로 정규화한다.

내부 결과의 `provenance`는 서버가 만든 다음 필드만 허용한다.

| 필드 | 의미 |
| --- | --- |
| `answer_hash` | 공백 등 원문을 보존한 choice/text/confidence canonical hash |
| `question_hash` | 문항 UUID·유형·보기·정답·프롬프트 등 문항 snapshot |
| `source_hash` | ID로 정렬한 실제 question_sources의 자료·페이지·인용 원문 |
| `material_hash` | 자료 metadata/revision 및 단원·개념의 구조/범위 snapshot |
| `model_version`, `prompt_version`, `schema_version` | 서버가 고정한 실제 채점 계약 식별자 |
| `saved_result_id` | AI gateway가 생성/재사용한 사용자 소유 결과 ID; 결정적 채점은 null |

해시는 lowercase SHA-256이다. unsupported JSON 숫자, NUL, surrogate, 임의 추가 필드 및 잘못된 참조 소유권은 저장 전에 거절한다. 같은 UUID의 source나 material이 바뀌어도 hash가 달라진다. 파일 내용을 매번 다시 읽어 검사하지는 않으며, 저장된 자료 metadata의 SHA/analysis_version/updated_at과 근거 원문을 버전 증거로 사용한다.

Q-05는 최종 확정 transaction 안에서 `validated_completed_result(connection, user_id, job_id)`를 호출해야 한다. 이 함수는 현재 입력과 provenance/저장 결과 소유권을 재검사하며, 미완료·변경·legacy provenance 누락은 `None`이다. 기존 성공 작업의 근거가 나중에 변경된 경우에도 성공을 추정하지 않는다. 해당 성공 작업을 새 버전으로 재채점하는 정책은 Q-05 연결에서 별도로 결정해야 한다. Q-04는 succeeded 작업을 자동 재실행하지 않는다. 과거 완료 세트 복구는 [Q-02 인계](quiz-storage-handoff.md)의 legacy 정책을 따른다.

## Q-03 접수와 영속 실행 모드

```python
GradingJobRepository(connection).enqueue(
    user_id, quiz_id, item_id, revision=revision, now=now, mode=None,
)
```

답안 저장과 enqueue는 같은 transaction에 두고 외부 작업 전에 commit한다. `0008_grading_execution_mode`는 `0007_preview_storage` 다음 migration이며, 기존 작업과 생략된 신규 insert에는 DB default `live`를 부여한다. 기존 배포 migration은 수정하지 않았다.

| 상황 | 동작 |
| --- | --- |
| 새 작업 + `mode=None` | live로 저장 |
| 기존 작업 + `mode=None` | 기존 live/saved/backup 그대로 유지 |
| 기존 작업 + 명시적으로 다른 mode | 내부 `ExecutionModeConflict`; enqueue에서 승격/변경하지 않음 |
| 자동 polling | queued 또는 expired-running만, 저장 mode로 실행 |
| failed | 자동 재시도 없음; 명시적 retry 필요 |
| 유효 running lease 중 retry | token/mode 유지, `NOT_CLAIMED`/처리 중 |
| queued/failed/expired-running의 명시적 retry | 현재 revision·상태·기존 token·mode를 조건으로 새 token과 mode를 원자 변경 |

명시적 retry는 `run_job(..., retry_failed=True, mode="saved")`처럼 호출한다. mode 생략은 여기서도 저장 mode를 유지한다. 자동 경로는 `retry_failed=False, mode=None`이며 기존 수동 `available()`은 failed도 포함하므로 자동 polling에 사용하지 않는다. low-level `claim()`의 retry_failed 기본 True는 Q-02 호출 호환용이다.

유효 live 실행이 이미 공급자 요청을 보냈다면 saved 요청이 그 전송을 취소하거나 과금이 없었다고 보장할 수 없다. 만료 후 명시적 mode 전환은 이전 token의 결과만 차단한다. 이후 자동 재시작은 영속된 saved/backup mode를 따르므로 provider/credential 초기화와 외부 전송이 모두 없다. 저장 결과가 없으면 실패를 저장하며 같은 실패를 자동 반복하지 않는다.

`GradingRun.state`는 `SUCCEEDED/FAILED/STALE/NOT_CLAIMED/MODE_CONFLICT/BUDGET_EXHAUSTED/IDLE`의 내부 enum이다. 특히 mode conflict를 공개 `AI_FAILED`로 미리 고정하지 않았다. Q-05가 기존 제출 API의 처리 중/재시도 응답과 연결한다. 내부 enum과 provenance를 FE에 그대로 노출할 필요는 없다.

### 부적격 퀴즈가 뒤 작업을 가리지 않는 탐색

`pending(now=..., limit=100, after=(created_at, id))`는 현재 답안에 대응하는 queued/expired-running 후보 한 페이지를 반환한다. 페이지 상한은 그대로이며 반환 행에는 탐색용 created_at/quiz_session_id가 추가된다. 개별 후보의 자료 소유권만으로 전체 퀴즈의 적격성을 추정하지 않는다. `run_next()`는 claim도 사용하는 `QuizAnswerRepository.items()`의 전체 퀴즈 검증을 먼저 수행하고, 같은 탐색에서 퀴즈별 검증 결과를 캐시한다. 따라서 Q-03의 concept 동일-unit 검증을 포함해 공유 소유권 규칙이 강화되면 polling에도 적용된다. claim은 잠금 아래에서 다시 검증한다.

손상된 형제 문항 때문에 부적격인 퀴즈는 건너뛰고 `(created_at, id)` keyset으로 다음 페이지까지 탐색한다. 선두 손상 작업 100개가 뒤의 정상 사용자 작업을 영구히 가리지 않는다. 이 과정은 잘못된 job을 삭제하거나 failed로 전환하지 않으며 답안·lease·execution_mode도 바꾸지 않는다. 예상된 NO_QUIZ만 부적격으로 다루고 예상 밖 DB/검증 오류는 숨기지 않는다.

`run_next()` 시작 시 만든 절대 GradingBudget을 탐색과 실제 run_job에 공유한다. 각 페이지 조회·전체 퀴즈 검증·선점 전에 잔여 시간과 종료 신호를 확인한다. 예산이 끝나면 BUDGET_EXHAUSTED를 반환하며 새 채점을 시작하지 않는다. 마지막으로 처리하거나 제외한 cursor는 같은 worker 인스턴스의 다음 polling에서 이어간다. 아직 선점하지 못한 정상 후보는 cursor 뒤에 남긴다. 큐 끝에서는 한 번 순환하여 앞쪽에 새로 생긴 작업도 찾는다.

cursor는 DB에 저장하는 작업 상태가 아닌 loop 프로세스의 탐색 위치다. worker 재시작이나 매번 새로운 `--once` 프로세스에서는 처음부터 다시 검사하며, 영속 job/mode에는 영향이 없다. deadline 검사는 이미 실행 중인 SQL 자체를 강제 취소하는 DB statement timeout을 대체하지 않는다. SQL이 반환한 뒤에는 남은 예산 없이 후속 탐색·선점·외부 요청을 시작하지 않는다.

## 제출 전체 시간 예산

Q-05는 한 제출에 `GradingBudget.start()`를 한 번 만들고 모든 문항에 같은 인스턴스를 전달한다. 기본은 20초이며 환경 설정도 공통 AI 예산 제한을 따른다.

```python
budget = GradingBudget.start()
with engine.begin() as connection:
    # 같은 transaction에서 답안/작업을 저장한 뒤 실행 준비
    prepared = worker.prepare(connection, user_id, job_id, budget=budget)
# caller commit 완료 후에만 실행
outcome = worker.execute(prepared)  # PreparedGrading인 경우에만 호출
```

`prepare()`가 반환한 `GradingRun`은 이미 처리 중/실패/예산 소진 등의 내부 결과이므로 execute 대상이 아니다. 간단한 독립 실행에는 `run_job(..., budget=budget)`를 사용한다. caller가 별도의 DB transaction이나 사용자 잠금을 잡은 상태에서 execute/run_job을 부르면 안 된다.

gateway의 provider·JSON syntax 재시도·검증 deadline은 이 절대 monotonic deadline 이하로 clamp한다. 느린 첫 문항 뒤 다음 문항에 새 20초를 부여하지 않는다. deadline 소진 뒤 새 외부 요청을 시작하지 않는다. gateway 프로세스 terminate/kill/join 정리 시간은 공통 설정에 따른 별도 제한이며, 정리 실패를 성공/일반 공급자 실패로 숨기지 않는다.

## 채점 및 AI adapter 계약

객관식/OX는 서버의 유일한 정답 보기 인덱스와 저장한 choice를 비교한다. 보기 문구가 중복되어 정답 인덱스를 특정할 수 없으면 잘못된 입력으로 실패한다. 단답은 앞뒤/연속 공백과 대소문자를 정규화하되 문장부호를 보존한다. 빈 단답은 오답이다. 빈 서술형은 외부 요청 없이 명시적 0점/오답이며, 공급자 실패의 `result=null`과 구별한다. 서술형 점수는 strict integer 0..100이고 85 이상 정답이다.

이 규칙은 [퀴즈 계약](api/quiz.md)을 따른다. 기존 vendor의 문자열 처리/실패 fallback에 의존하지 않으며, 실패를 0점으로 확정하지 않는다. FE의 bulk submit 필드 choice/text는 유지한다. 기존 FE가 bulk submit만 호출하는 차이와 문항별 저장 경로 연결은 Q-03/Q-05에서 처리한다.

고정 AI submodule `6d74ffb6c455a306e912252013d76f93e30a6210`의 실제 `essay_grader.grade_essay(..., groq_service=...)`에 adapter를 주입한다. vendor의 6회 retry/25초 sleep 클라이언트는 초기화하지 않는다. `openai/gpt-oss-120b`, 고정 prompt/schema version을 결과 identity에 넣는다. 이 모델 문자열은 공급자 모델 식별자이며 서비스의 불변 가중치 증명을 뜻하지 않는다.

전송은 SDK `max_retries=0`, HTTP transport retries=0, redirect 비활성화다. 각 실제 SDK 전송 직전에 남은 timeout과 `start_request`, 완료/실패에 `finish_request`를 연결한다. 공급자 원문 JSON을 gateway로 반환해 JSON syntax 오류만 남은 전체 예산에서 한 번 재시도하며, 의미/범위/구조/근거 오류는 반복하지 않는다. 근거는 실제 소유 자료의 페이지와 nonblank 인용문이 필요하며 임의 page 1을 만들지 않는다. pinned prompt builder는 source_text의 처음 9,000자를 사용하므로 긴 인용의 후반부까지 AI가 읽었다고 보장하지 않는다.

gateway는 owner·canonical 입력·근거/자료 revision·model/prompt/schema version이 맞는 검증 결과를 재사용한다. saved/backup miss는 자격 확인 전 실패하고 provider를 초기화하지 않는다. 공용 user-null demo cache를 이 worker에서 허용하지 않는다. `GatewayRequest.job_id=None`을 사용한다. quiz_grading_jobs ID는 ai_jobs FK가 아니므로 usage에 잘못 넣지 않는다. 결과 연결은 provenance의 saved_result_id와 각 저장 row의 입력 hash로 추적한다.

usage는 관측된 실제 전송만 남기며 응답에 없는 token/cost는 null이다. 이 hook은 주입된 transport의 관측 계약이며 임의 라이브러리의 숨은 네트워크를 자동 감지하는 기능이 아니다. 실키 없이 실제 pinned 함수와 fake SDK/HTTP transport를 검증했고, live AI 품질·과금·실측 응답시간을 검증했다고 주장하지 않는다.

## CLI와 배포 실행

```sh
python -m app.workers.grading --once
python -m app.workers.grading --loop
python -m app.workers.grading --retry-job JOB_UUID --user-id USER_UUID --retry-mode saved
python -m app.workers.grading --healthcheck
```

CLI는 시작 시 DB head를 확인한다. once는 최대 한 작업을 처리하고 loop는 queued/expired-running만 polling한다. SIGTERM/SIGINT를 받으면 새 선점을 멈추고 현재 bounded 작업과 child 정리를 마친 뒤 종료한다. `CleanupFailed`는 비정상 exit 1로 supervisor 교체를 요구한다. 로그는 job ID/상태/내부 오류 코드 또는 예외 클래스만 기록하고 답안/키/SDK 예외 본문을 출력하지 않는다. heartbeat healthcheck는 DB/provider 초기화 없이 loop PID와 만료 시각을 확인한다.

Compose의 grading-worker는 API와 같은 고정 BE_IMAGE, runtime 환경, 저장소 volume을 사용하고 새 포트를 열지 않는다. restart 정책은 unless-stopped, 종료 유예는 35초다. worker와 API/migrate에 DB writer label을 붙인다.

`deploy/apply.sh`는 기존 image SHA/AI label 확인을 writer 정지보다 먼저 수행한다. 새 이미지의 worker import probe는 예상된 모듈 부재만 exit 42로 구분하며, 의존성 누락/실제 import 오류는 현재 서비스를 유지한 채 실패한다. 최신 script와 과거 worker 없는 Compose 조합도 지원한다. exact Compose project label의 API·worker·migrate/명시적 writer를 찾아 orphan worker까지 정지한 후 pg_dump와 migration을 수행한다. 다른 project와 DB는 정지하지 않는다. 새 이미지에 모듈이 있으면 worker service도 있어야 한다. 정상 migration 뒤 API와 해당 이미지가 지원하는 worker를 기동한다.

이는 과거 이미지의 module/Compose 호환 경계이며 DB downgrade를 자동 수행한다는 뜻이 아니다. `0008` downgrade는 영속 실행 mode를 제거하므로 saved/backup 의도를 잃는다. 신규 작업이 쌓인 운영 DB에 downgrade하기 전 백업/복구 정책이 필요하다. 실제 배포와 RPI 조작은 수행하지 않았다. 기존 배포 앱 `4c20454`는 worker 미연결 버전이므로 이번 worker의 운영 검증 근거가 아니다. 후속 배포에서 모든 writer 정지 → 백업 경계를 다시 확인해야 한다.

## 검증

SQLite와 PostgreSQL에서 원자 claim/소유권, 중복 worker, crash/만료 lease, 변경 답안·문항·근거·자료, rollback, SQL NULL, saved/backup mode CAS, legacy provenance, strict 결과와 경계를 검증한다. 실제 CLI subprocess로 SIGTERM/SIGINT 뒤 provider child 소멸, CleanupFailed nonzero, expired saved/backup 재시작의 외부 0회 및 실패 자동 재시도 없음도 확인한다. 배포 테스트는 mock Docker와 실제 shell/Python probe로 수행한다.

독립 검토의 손상 퀴즈 starvation 재현 파일은 변경 없이 두 DB에서 2 passed로 재확인했다. 추가 polling 검증은 전체 퀴즈 검사 캐시, 같은 created_at의 ID 순서, 페이지 진행, stop/예산 소진 뒤 재개, 미선점 정상 후보 보존, cursor 순환을 포함한다. 보정 후 polling·worker·mode·CLI·채점 저장소·Q-02 저장소 회귀는 361 passed였으며 실제 외부 AI를 호출하지 않았다.

재현 명령은 다음과 같다. PostgreSQL URL은 운영 DB가 아닌 전용 로컬 테스트 DB를 사용한다.

```sh
TEST_POSTGRES_URL=postgresql+psycopg://postgres:team10-local-test@127.0.0.1:54511/team10_test \
  .venv/bin/python -m pytest -q \
  tests/test_grading_repository.py tests/test_grading_adapter.py tests/test_grading_worker.py tests/test_grading_polling.py \
  tests/test_grading_modes.py tests/test_grading_mode_migration.py tests/test_grading_worker_cli.py \
  tests/test_quiz_storage.py tests/test_quiz_storage_migration.py tests/test_ai_gateway.py \
  tests/test_ai_dependency.py tests/test_essay_failure.py tests/test_proposal_storage_migration.py \
  tests/test_migrations.py tests/test_database_startup.py tests/test_deployment.py
node scripts/gen_schema/test_gen_schema.js
```
