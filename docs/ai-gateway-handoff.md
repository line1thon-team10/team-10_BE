# AI-01 공통 결과 경로 인계

2026-11-14. 내부 공통 경로 구현이며 HTTP endpoint, PDF worker, 퀴즈 채점 worker, 실제 공급자 adapter 연결 완료를 뜻하지 않는다. 기존 `ai_saved_results`, `ai_usage_logs`를 사용하며 schema·migration·기존 quiz 서비스는 변경하지 않는다.

## 호출 인터페이스와 책임

- `ResultIdentity.create(kind, provider, model_version, prompt_version, schema_version, input, validation_context, materials, scenario_key)`는 입력을 canonical JSON으로 고정한다. 객체 키 순서는 정규화하고 배열 순서·값 타입·문자열 내용은 보존한다. `MaterialRevision(material_id, revision)`은 자료별 구조 revision을 담는다.
- `GatewayRequest(owner_id, identity, job_id=None, pdf_job=False)`는 호출 소유자와 선택적 작업을 고정한다. `AiGateway(engine, settings=..., approved_demos=...).run(request, validator=FunctionRef(...), provider=ProviderSpec(...), mode="live"|"saved"|"backup")`가 진입점이다.
- `GatewayResult`에는 검증된 `data`, `saved_result_id`, `source`(`live`, `private_cache`, `demo_cache`), 이번 실행에서 관측한 `actual_call_count`가 있다. HTTP의 `saved:true` 또는 `essayGrading:"saved"` 매핑은 각 endpoint adapter가 맡는다.
- provider·validator `FunctionRef(module, name)`는 import 가능한 서버 고정 함수다. HTTP 값, AI 출력, 업로드 파일로 함수 경로를 고르지 않는다. validator는 `(value, validation_context) -> dict|list`, provider는 `(ProviderCall, ProviderContext) -> JSON 문자열 또는 dict|list`다. 프로세스로 전달하는 객체에 SDK client, DB connection, closure를 넣지 않는다.
- caller가 기존 답안/작업 접수를 먼저 commit하고 사용자 행 잠금과 트랜잭션을 해제해야 한다. Gateway는 전달받은 Engine에서 짧은 연결을 열고 닫으며, caller의 Connection을 받거나 임의로 닫지 않는다. 외부 실행 중 Gateway의 DB 연결은 없다. Engine 자체도 dispose하지 않는다.
- 반환 뒤 현재 답안 revision·자료 구조 revision·작업 lease를 다시 잠금 아래 확인하고 도메인 상태에 적용하는 책임은 Q-04/M-02 등 worker에 있다. Gateway는 전달된 자료 revision이 DB의 최신 revision이라고 추정하지 않는다. 자료→과목→사용자와 선택적 job의 소유자·kind는 캐시 접근 전에 확인한다.

## 저장 결과와 공개 데모

해시는 `ai-input-v1` envelope의 kind, provider, model/prompt/schema version, canonical 입력, validation snapshot, 정렬된 자료 ID/revision, scenario를 모두 포함한다. 소유자는 해시에 합치지 않고 SQL의 `user_id` 조건으로 격리한다. 개인 결과가 승인된 공용 결과보다 우선이다. 재접속 조회와 저장 결과 재사용에도 같은 domain validator를 실행한다.

`mode="saved"` 및 `"backup"`은 일치 결과가 없으면 provider import·자격 확인 전에 `503 AI_FAILED`다. 따라서 키가 없는 환경에서도 일치 저장 결과를 사용할 수 있다. live 모드도 캐시가 있으면 외부 호출 없이 사용한다. 저장 결과가 현재 validator를 통과하지 못하면 live fallback을 시작하지 않는다.

`user_id=null`은 `DemoApproval(scenario_key, exact_input_hash, self_authored=True)`가 서버 allowlist에 있는 자체 제작 데모만 가능하다. `register_demo`는 외부 요청 없이 validator를 통과한 결과를 저장한다. 개인 DB 자료 ID가 포함된 identity는 공용으로 게시할 수 없다. 공용 데모는 고정 자체 제작 snapshot을 입력/validation context로 식별한다. 단순 demo 사용자, 유사 문항, 같은 code 또는 null owner만으로 재사용하지 않는다. 기존 시드/채점 예제는 자동 변환하지 않았다.

## 검증과 재시도

공통 검사는 중첩된 NaN/Infinity/overflow float, JSON 이외의 타입, 순환 객체, 문자열이 아닌 객체 키, 중복 JSON 키, 인코딩할 수 없는 문자열을 거절한다. PostgreSQL JSONB/Text에서 표현할 수 없는 U+0000은 값·키·provider 식별자에서도 거절해 SQLite와 같은 결과를 낸다. 최종 루트는 object 또는 array다. 값의 의미·근거·자료 범위·점수·문항 구조와 OCR 품질(POL-03)은 각 domain validator가 고정 snapshot으로 검사한다. 공통 경로 자체가 OCR이나 정답 품질을 검증했다고 표시하지 않는다.

실제 transport 성공 뒤 반환된 문자열의 `JSONDecodeError`만 한 번 재시도한다. 두 번째 시도에는 `ProviderCall.format_retry=True`가 전달된다. 자격 누락, 권한 오류, 실패 transport, NaN, 잘못된 범위/근거/구조, validator 오류는 재시도하지 않는다. SDK가 문자열을 먼저 파싱해 예외를 숨기지 않도록 adapter는 원문 JSON 문자열을 반환하거나 이미 얻은 JSON 값을 전달한다.

## 시간과 종료

`AiSettings`는 환경의 `AI_REQUEST_BUDGET_SECONDS`(기본20, 양수·최대25), `AI_PDF_JOB_BUDGET_SECONDS`(기본300, 양수)를 별도로 검증한다. NaN/무한/표현 불가능한 값은 거절한다. PDF 예산은 `kind="pdf_analysis"`와 `pdf_job=True`를 명시한 백그라운드 실행에만 적용한다. 일반 PDF 동기 요청에 자동으로300초를 주지 않는다.

하나의 monotonic deadline 안에 spawn, 공급자 호출 전체, 최대 한 번 JSON 재시도, domain validation, 종료 정리를 묶는다. 정리를 위해 남은 시간의 최대10%와 `cleanup_timeout_seconds`(기본1초) 중 작은 값을 미리 남긴다. 모든 transport는 `context.remaining_seconds`를 timeout으로 사용해야 한다. 작업마다 예산을 새로 만들지 않는다.

실행은 POSIX spawn 프로세스에서 이루어진다. 완료·실패·timeout 뒤 해당 process group에 SIGKILL을 보내고 join으로 종료를 확인한다. SIGTERM을 무시하는 SDK도 종료 대상이다. parent는 비차단 framed pipe를 사용하므로 일부만 전송된 큰 출력에서 blocking recv로 멈추지 않는다. 출력 frame8MiB, telemetry4096개는 IPC 자원 보호 경계이며 제품의 PDF/문항 한도가 아니다. 초과하면 성공으로 저장하지 않는다.

OS가 join 예산 내 종료를 확인하지 못하면 `CleanupFailed`를 올린다. 이 경우 worker health 실패로 취급해 supervisor가 worker를 교체해야 하며 성공/일반 AI 실패로 숨기지 않는다. 신뢰된 adapter는 자체 daemon/분리 session 또는 별도 background worker를 만들지 않는다. 이 경계는 악성 Python 코드를 가두는 보안 sandbox가 아니다. 로컬 실행 종료는 공급자에게 이미 전송된 요청이나 그 비용의 취소를 보장하지 않는다.

DB cache 조회와 사후 usage/cache 저장 지연은 DB 운영 timeout 정책의 적용 대상이다. 이 구현은 외부 실행의 deadline과 child 종료를 검증하며 전체 HTTP 지연이나 공급자 응답 성능을 실측 보장하지 않는다. 사용량 저장은 실행 종료 뒤 별도 commit하며, 그 뒤 캐시 저장 실패가 발생해도 이미 관측한 요청 기록을 지우지 않는다.

## 실제 transport 사용량 계약

`ProviderSpec.sdk_max_retries`는 정확히 정수0이어야 한다. adapter는 SDK 및 transport의 자동 재시도를 끄고 아래 훅을 **실제 전송 지점마다** 연결해야 한다.

1. 자격 확인, 입력 준비를 마친 뒤 실제 요청 직전에 `request_id = context.start_request(provider=..., model=...)`.
2. 응답 또는 실패에서 `context.finish_request(request_id, status="succeeded"|"failed", provider_request_id=..., input_tokens=..., output_tokens=..., estimated_cost=..., currency="USD")`.
3. 실패한 요청은 실패로 마친 뒤 예외를 전달한다. 토큰/비용을 공급자가 제공하지 않으면 `None`을 유지한다. finish의 latency는 실제 시작/끝 monotonic 간격으로 계산한다.

여러 내부 요청을 하는 library 함수를 한 번 감싼 훅은 충분하지 않다. 해당 library가 쓰는 SDK/HTTP transport에 훅을 연결해 요청 수만큼 기록해야 한다. 중첩 호출의 provider/model도 각 행에 보존한다. 누락 훅, 미종료 요청, 중복/위조 request ID, 음수·bool 토큰 수 등 잘못된 telemetry를 정상 결과로 채택하지 않는다. 이 훅은 신뢰된 adapter의 관측 계약이며 숨겨진 네트워크 호출을 자동 탐지하는 기능이 아니다.

사용량에는 parent가 정한 사용자/job/kind만 넣는다. cache hit/miss, 키 누락, validator-only 실행은 사용량0건이다. 응답 JSON이 잘못됐어도 실제 transport가 성공했다면 그 요청은 `succeeded`로 기록하고 결과 저장은 실패한다. timeout/프로세스 비정상 종료로 끝 이벤트가 없는 실제 시작 요청은 `failed`, 알 수 없는 토큰·비용·latency는 null이다. 진행 중 자료가 삭제되더라도 사용량은 보존하고 결과 저장은 소유권 검증에서 실패한다. job이 삭제되었으면 다른 작업에 연결하지 않고 `job_id=null`로 남긴다.

## 후속 연결과 검증 범위

- 현재 private AI의 `services/groq_client.py`는 자체 `MAX_RETRIES=6`, `MIN_REQUEST_INTERVAL=25`를 사용하고 transport hook이 없다. 이 클라이언트를 그대로 gateway provider로 연결하면 계약을 충족하지 않는다. 후속 adapter에서 SDK retry0·남은 timeout·요청별 usage hook을 지원해야 한다. vendor를 이번 task에서 수정하지 않았다.
- Q-04는 답안 revision/lease, M-02는 PDF 단계·실패·lease, O-03/T-01은 자연어·튜터 domain validator와 HTTP 응답 매핑을 연결한다. API의 기존 경로가 자동으로 이 gateway를 쓰도록 변경하지 않았다.
- `tests/test_ai_gateway.py`는 SQLite/PostgreSQL의 캐시·소유권·usage와 synthetic transport의 취소·형식 재시도·근거/범위 검사·큰 출력·부분 프레임·비정상 종료를 검증한다. 기존 `test_ai_dependency.py`, `test_essay_failure.py`도 회귀 대상이다.
- 모든 공급자 실행 테스트는 로컬 fake다. 실제 API 키 사용, 과금 호출, OCR/채점/튜터 품질 또는 실제 모델 지연 측정은 수행하지 않았다.
- 독립 검토에서 유효 JSON의 U+0000이 SQLite에는 저장되지만 PostgreSQL에서는 SQL 오류로 빠지는 결함을 재현했다. 저장 전에 공통 경계에서 거절하도록 보완했다. 잘못된 결과는503 AI_FAILED이며 완료된 실제 transport 사용량은 유지한다. 잘못된 provider request ID 때문에 완료 이벤트를 해석할 수 없으면 시작된 요청을 failed/null로 남긴다. 수정 후 두 DB의 관련 회귀161개가 통과했다.
