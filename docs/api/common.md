# 공통 계약

## HTTP와 인증

- 기본 경로 `/api`; FE와 같은 origin의 프록시를 기본으로 사용한다. 별도 origin 배포 시 지정된 FE origin만 CORS 허용한다.
- `POST /api/session`만 익명 ID 없이 호출한다. 성공은 `200 {"success":true,"data":{"anonymousId":"UUID"}}`.
- 이후 모든 계약 경로는 `X-Anonymous-Id` 필수. 누락 `401 NO_SESSION`, 형식 오류/미등록 `401 UNKNOWN_SESSION`.
- 빈 헤더 값은 누락과 동일하게 처리한다. 헤더를 둘 이상 보내거나 여러 ID를 쉼표로 결합하면 `401 UNKNOWN_SESSION`이며 어느 하나를 임의 선택하지 않는다. UUID는 DB 조회 전에 파싱·정규화한다(대문자/하이픈 없는 동일 UUID도 동일 사용자). FE는 서버가 발급한 문자열을 그대로 보관·전송하면 된다.
- 현 해커톤 계약에서 이 헤더는 소지 기반 세션 자격이다. 로그/공개 응답/다른 사용자 응답에 노출하지 않는다. ERD의 'ID는 인증 비밀값 아님' 설명과는 다르며 운영용 인증으로 승격하려면 별도 세션 토큰 설계가 필요하다. 이번 버전은 UUID만 아는 제삼자를 구별할 수 없음을 명시한다.
- 다른 사용자의 객체는 해당 도메인의 `404 *_NOT_FOUND`로 응답한다. 자식 객체의 상위 과목·자료·계획 소유권도 확인한다.
- JSON 성공은 `200 {"success":true,"data":...}`. PDF만 200 binary. 204/202는 사용하지 않는다. 읽지 않는 mutation의 data도 생략하지 않고 도메인 문서에 정한다.
- JSON 실패는 `{ "success": false, "error": { "code": "...", "message": "한국어 안내" } }`. 내부 경로, SQL, API 키는 message에 넣지 않는다.
- 응답은 `Cache-Control: no-store`. PDF도 개인 자료로 처리한다.

## 타입·필수값·범위

- ID는 불투명 string. 서버 발급은 UUID; FE는 ID를 파싱하거나 `ds` 같은 값으로 고정하지 않는다. 일정 회차 ID만 `UUID@YYYY-MM-DD`.
- 필드 표의 `–`는 아래 사전으로 타입을 정한다. `id/*Id`, `name/title/label/text/note/reason/grade/status/type`, 날짜/시각은 string. `no/unitNo/count/pages/minutes/tasks/total/score/rate/round`는 number. `from/to`는 페이지 객체에서는 정수, 캘린더 조회에서는 날짜 string이다.
- 페이지·개수·요일·인덱스는 정수. 점수·백분율·분당 속도는 유한 number. NaN/Infinity 금지. 페이지는 1부터 양끝 포함, 보기 choice는 0부터.
- 날짜 `YYYY-MM-DD`, 월 `YYYY-MM`, 시각 `HH:mm`; 실제 존재하는 값만 허용. 사용자 시간대 Asia/Seoul. DB timestamp는 timezone 포함, 날짜 집계는 사용자 시간대 기준.
- 일반 사용자의 today는 서버 현재 날짜, 데모는 users.demo_date. 자정 처리는 last_assessed_date 이후 미처리 날짜를 순서대로 한 번씩 처리한다.
- 목록은 항상 배열(없으면 `[]`); null 배열 금지. 표에서 nullable인 scalar/object는 항상 키와 null을 반환한다. 기본값이 표시된 bool/array/number는 null 대신 그 기본값을 반환한다. 비nullable 키는 생략하지 않는다. mockFilled는 서버에서 생략 가능하며 FE 기본 []를 사용한다.
- 요청의 선택 필드는 생략 가능. null은 명시적으로 nullable인 필드만 허용. 알 수 없는 요청 키는 422 INVALID_INPUT. PATCH는 전송된 키만 변경한다.
- 자유 text/제목/배열의 일률적인 제품 상한을 두지 않는다. 엔드포인트별 명시된 제한만 계약으로 적용한다. 배포 시 요청 크기 제한은 운영 설정으로 별도 공개한다. 공백만 있는 필수 text는 `400 EMPTY`. 도메인별 좁은 제한이 우선한다.
- 숫자는 입력 문자열을 암묵 변환하지 않는다. bool을 정수로 받지 않는다. 선택 enum 밖의 값은 422 INVALID_INPUT.
- 조회 정렬: 날짜 오름차순 → 도메인 sort_order → ID. 오답 기록은 마지막 풀이 날짜 내림차순 → ID. 동점 순서는 고정한다.

## 오류 상태

기존 FE error.code를 유지한다. [오류 사전](errors.md)에 없는 내부 오류는 500 SERVER_ERROR.

- 400: 의미적으로 실행할 수 없는 입력/상태 (`BAD_*`, `EMPTY`, `CONFIDENCE_REQUIRED`, `NO_DAYS` 등).
- 401: 익명 세션 누락/오류.
- 403: QUIZ_LOCKED, DEMO_DISABLED.
- 404: 리소스 없음/다른 사용자, NO_PLAN(today), NO_RESULT(result), NO_QUIZ, NO_FILE, NO_RESPLIT, NO_REPLAN(options).
- 409: ALREADY, OTHER_ACTIVE, ENDED, NOT_ACTIVE, QUIZ_FINALIZED, PREVIEW_STALE, PREVIEW_REQUIRED, IDEMPOTENCY_CONFLICT, SESSION_USED, SESSION_RUNNING.
- 413 FILE_TOO_LARGE, 415 NOT_PDF, 422 INVALID_INPUT, 429 RATE_LIMITED, 503 AI_FAILED.
- NO_PLAN(next-day), NO_RESULT(more), NO_REPLAN(apply), NO_PREVIEW는 400. 위 예외를 제외한 NO_* 중 조회 대상 부재는 404, mutation 선행조건 부재는 400.
- FE는 새 code를 모르는 경우에도 한국어 message를 표시한다. `ECONNABORTED`는 클라이언트 오류이며 서버 code로 보내지 않는다.

## 트랜잭션과 중복 요청

일반 mutation은 모든 DB 변경을 원자적으로 적용한다. 오류 시 일부 변경을 남기지 않는다. 예외는 채점의 답안 선저장(quiz.md)과 PDF 작업 접수(material.md)다.

선택적 `Idempotency-Key` 헤더: 사용자가 의도한 요청마다 UUID, 네트워크 재시도에는 같은 값. 서버는 `(user, method, path, key)`로 본문 해시와 완료 응답을 보관한다(보관 기간은 운영 정책; 도메인의 중복 방지 기록은 별도로 유지). 같은 키·다른 본문은 409 IDEMPOTENCY_CONFLICT. 진행 중이면 409 REQUEST_IN_PROGRESS(추가 공통 코드), 성공한 요청은 저장된 응답 재전송. 실패한 AI 작업은 같은 키로 재시도 가능. 트랜잭션에 영향을 준 채점 실패는 quiz.md 규칙으로 처리한다.

키 없이도 퀴즈 제출/집중 시작/계획 적용은 도메인 식별자와 잠금으로 중복 효과를 방지한다. 키 없는 세션 생성·메시지·next-day 등은 별개의 요청으로 본다. FE 자동 mutation 재시도는 기본 금지하며 위 헤더를 도입한 호출만 허용한다. 이 헤더는 11/12 팀 합의가 아니라 선택적 보완안이다.

## AI와 저장 결과

- FE timeout 30초 유지. 동기 AI 작업의 기본 총 예산은 운영 설정 `AI_REQUEST_BUDGET_SECONDS=20`, PDF 백그라운드 분석은 `AI_PDF_JOB_BUDGET_SECONDS=300`이다(POL-02). 이는 이번 배포용 초기 설정이며 제품 요구나 실측 성능 보장이 아니다. 동기 값은 양수이고 25초 이하로 제한해 FE 응답 여유를 둔다. 공통 Job/202는 추가하지 않는다.
- PDF만 기존 `/materials/jobs/:jobId`로 실제 단계 조회. 나머지 FE 단계 표시는 대기 연출이며 진행률로 해석하지 않는다.
- 저장 결과는 사용자·입력 해시·자료 구조 버전·모델/프롬프트 버전이 맞는 결과만 재사용. 미리 검증된 데모 결과는 해당 데모 자료/문항/답안에만 적용.
- `saved=1`, `saved:true`, `savedGrading:true`, tutor backup은 외부 AI 호출을 금지한다. 일치하는 저장 결과가 없으면 503 AI_FAILED. 임의 응답에 saved만 붙이지 않는다.
- 저장된 채점만 `essayGrading:"saved"`; 나머지 지원 응답은 `saved:true`. 일반 계산 결과는 saved:false.
- **NF-04, 원문 p.21 / 와이어 상태 메모:** AI 출력 JSON 형식이 깨지면1회 재시도하고 다시 실패하면 안내한다. AI가 반환한 범위·문항·근거·점수는 서버가 소유권과 유효성을 검증한 후 저장한다. 잘못된 출력은 AI_FAILED이며 기존 승인 데이터는 유지한다.

### AI 운영 예산의 적용 경계 (POL-02)

- 시작 시 monotonic deadline 하나를 만들고 공급자 요청·JSON 재시도·출력 검증을 모두 같은 예산에서 처리한다. 한 문항 또는 재시도마다 20초를 새로 부여하지 않는다. 예산이 소진되면 새 외부 요청을 시작하지 않고 `503 AI_FAILED`를 반환한다. 이미 저장한 답안은 유지하며 늦게 도착한 결과는 동일 입력 revision/작업 lease를 다시 확인한 뒤에만 채택한다.
- JSON 형식 실패는 남은 예산 안에서 최대 한 번 다시 요청한다. 자격 누락·권한 실패·의미적으로 잘못된 출력은 반복 호출로 숨기지 않는다. 공급자 자체 자동 재시도도 이 예산과 횟수를 넘겨서는 안 된다. 취소할 수 없는 작업을 단순 background thread에 남겨놓고 제한을 지켰다고 주장하지 않는다.
- PDF는 접수 응답을 기다리는 동기 작업과 분리한다. 작업 단계·실패·lease를 저장하며 300초 경과/프로세스 재시작은 재시도 가능한 실패로 처리한다. DB 트랜잭션/사용자 잠금을 잡은 채 외부 요청을 기다리지 않는다.
- 서버 내부의 요청 예산과 사용 요금 한도는 별개다. 실제 공급자 usage가 없으면 token/cost를 추정해 실측처럼 저장하지 않는다. 키 없는 배포에서는 저장된 결과와 결정적 계산만 가능하며 실제 AI 품질 검증 완료로 간주하지 않는다.
