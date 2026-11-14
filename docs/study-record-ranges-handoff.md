# 범위별 기록 API 전달 사항 (R-02)

기준 계약은 [study.md](api/study.md)와 [common.md](api/common.md)다. 이번 변경은 `POST /api/records`와 `PATCH /api/tasks/:id/record`의 요청 검증·전체 교체 저장·정정 응답을 연결한다.

## FE 요청 변경

- `taskId`, `rangeId`, `sessionId`는 서버가 발급한 UUID 문자열을 보낸다. 페이지는 양의 정수이며 문자열·실수·bool은 허용하지 않는다. 알 수 없는 키도 `422 INVALID_INPUT`이다.
- 선택 필드는 사용하지 않으면 생략한다. 현재 `S7Record.jsx`의 기본 `sessionId:null`은 전송 전에 제거해야 한다. 실제 집중 기록이 있을 때만 해당 문자열을 보낸다.
- `completedRanges:[{rangeId,toPage}]`는 전체 교체 스냅샷이다. 각 범위의 시작부터 `toPage`까지 기록하며, 새 요청에 없는 이전 완료 근거는 제거한다. 중복 rangeId와 다른 태스크의 rangeId는 `422 INVALID_INPUT`이다.
- `toPage`와 `completedRanges`를 함께 보내지 않는다. `toPage`는 실제 범위가 하나인 태스크에만 사용할 수 있다.

## 기록과 정정

POST의 `result:"partial"`은 고유 완료량이 0보다 크고 전체보다 적어야 한다. 동일 PDF의 겹친 페이지는 한 번, 다른 PDF의 같은 쪽은 각각 센다. 마지막 페이지에 도달했어도 중간 또는 다른 PDF의 페이지가 빠지면 완료가 아니다.

POST의 `done`은 범위 입력을 생략하면 전체를, `none`은 생략하면 빈 범위를 기록한다. `completedRanges`를 함께 보냈다면 실제 완료량이 선언한 결과와 일치해야 한다. POST의 `toPage`는 `partial` 호환 입력이므로 전체 완료에는 `result:"done"`을 사용하고 `toPage`를 생략한다. 페이지 없는 태스크의 partial은 `400 BAD_PAGE`이며 done/none은 가능하다.

PATCH는 `toPage` 또는 `completedRanges` 중 정확히 하나가 필요하다. `completedRanges:[]`는 0쪽 정정이며 `status:"none"`이 된다. 전체 합집합을 채우면 done, 나머지는 partial이다. `toPage:0`은 페이지 범위 밖이므로 허용하지 않는다. 기록이 없으면 `400 NOT_RECORDED`이며, none으로 기록한 태스크도 다시 정정할 수 있다.

PATCH 응답은 `taskId`, `status`, `doneTo`, `donePages`, `ranges`, `rate`를 반환한다. ranges는 서버 배치 순서의 `{id,materialId,unitId,conceptId,from,to,doneTo}`다. 복합 범위의 최상위 doneTo는 null이고 donePages는 자료별 합집합이다. 각 범위의 doneTo는 같은 단원의 완료 근거가 해당 시작 페이지부터 연속해 덮는 마지막 페이지다. 뒤쪽 섬만 완료했으면 앞부분까지 완료한 것으로 표시하지 않는다.

정정 시 기존 기록 ID·기록일·최초 기록 시각·연결된 집중 세션을 보존한다. 이미 저장된 회독의 recordIds/requiredRanges만 재검사하고 새 회독은 만들지 않는다. 오류 시 범위·기록·태스크·회독 변경을 함께 롤백한다.

## 후속 연결 경계와 검증

GET today/focus의 공통 TaskView는 R-03, 전체 진행률·집계 교체는 D-01, Idempotency-Key 응답 보관은 R-04다. 이번 변경의 POST event.rate와 PATCH rate는 기존 진행률 계산을 유지한다. 페이지 없는 명목 분량의 회독 근거 형식도 추가하지 않는다.

`TEST_POSTGRES_URL`을 독립 테스트 DB로 설정한 환경에서 `.venv/bin/python -m pytest -q tests/test_record_ranges.py tests/test_record_transactions.py tests/test_record_sessions.py tests/test_learning_evidence.py`는 SQLite·PostgreSQL 합계 223개를 통과했다. 데모 집중→기록→퀴즈 회귀인 `tests/test_demo_package.py tests/test_demo_quiz_flow.py`는 두 DB 합계 22개를 통과했다.
