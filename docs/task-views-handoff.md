# R-03 홈·집중 범위 조회 인계

`GET /api/today`의 `tasks[]`와 집중 API 여섯 경로의 `task`가
`app/services/task_views.py::TaskView`를 사용한다. 집중의 `FocusTask` 응답 모델에도
`donePages`, `doneTo`, `unitNo`를 추가했다. 기존 타이머, 세션별 예상 시간·문구 스냅샷,
기록 연결 여부, 보관 과목·retire 단원의 과거 세션 조회는 유지한다.

## 공통 필드와 계산 경계

- `ranges`는 서버 배치 순서의 `{id, materialId, unitId, conceptId, from, to, doneTo}`다.
  범위 식별자는 `study_task_ranges.id`이며, 중복되는 페이지라도 범위 행·개념 식별자를 합치지 않는다.
- `pages`는 같은 `materialId` 안의 합집합이다. 서로 다른 PDF의 같은 쪽 번호는 별개다.
  R-01 구간 연산을 사용하므로 쪽마다 집합 원소를 만들지 않는다.
- `donePages`는 **이 태스크의 최신 기록 스냅샷**을 해당 `unitId`의 계획 범위와 교차한 뒤
  자료별 합집합으로 센다. 다른 태스크/회차/단원의 완료나 계획 밖 페이지를 섞지 않는다.
- 범위의 `doneTo`는 시작점부터 연속으로 완료한 마지막 쪽이다. 뒤쪽 완료 섬은
  `donePages`에는 포함하지만 빈틈을 건너 `doneTo`를 늘리지 않는다. 시작점이 미완료면 null이다.
  R-02의 `record_range_view`와 같은 의미이며, POST/PATCH 전체 교체 뒤 홈 조회도 검증했다.
- 최상위 `from`, `to`, `unitNo`, `doneTo`는 페이지 범위가 하나일 때만 제공한다.
  복합 범위와 페이지 범위+명목 범위의 혼합은 null이다. 단일 범위의 기존 숫자 표시는 유지한다.
- 페이지 없는 명목 태스크/빈 범위는 `ranges:[]`, `pages:0`, `donePages:0`이다.
  완료 상태는 기록 result/태스크 status로 유지하며, 0쪽이라고 자동 완료하지 않는다.
  반대로 `status=completed`만으로 완료 범위나 `doneTo`를 만들어내지 않는다.
- 집중의 기본 `material`은 첫 번째 페이지 범위의 자료 ID와 시작 쪽이다. 범위가 없으면 null이다.

예를 들어 A 자료 12–16, A 자료 15–20, B 자료 12–16의 계획량은 14쪽이다.
A 자료의 15–17과 B 자료의 12–13을 끝냈다면 완료량은 5쪽이다.
첫 A 범위는 시작점 12가 미완료이므로 `doneTo:null`, 두 번째 A 범위는 `doneTo:17`이다.

`StudyData`는 범위 SQL에서 자료 ID와 단원 code를 읽는다. 활성 단원 목록에서 retire 단원이
빠져도 저장된 태스크의 범위 식별자는 사라지지 않는다. 태스크의 사용자·과목과 다른 자료는
조회 범위에 넣지 않는다. 활성 과목/단원 목록을 쓰는 기존 정책 계산 자체는 변경하지 않았다.

## FE와 기존 BE의 차이

- 기존 BE 홈은 `pages/ranges`가 없고 범위 길이를 단순 합산했다. 복합 범위에도 첫 시작 쪽과
  마지막 끝 쪽을 단일 범위처럼 반환했다. 이번 변경은 공개 study 범위 계약에 맞춘 보정이다.
- 기존 BE 집중은 별도 쪽별 집합 계산을 사용했고, 뒤쪽 완료 섬의 끝을 `doneTo`로 노출했다.
  공통 계산으로 교체하고 실제 HTTP 응답 모델을 통과한 완료량/표시 필드까지 검증했다.
- 확인한 `FE/src/api/adapters/today.js::toTask`는 여전히 `raw.to - raw.from + 1`로
  `pages`를 만들고 `ranges`를 전달하지 않는다. `raw.pages`, `raw.donePages`, `raw.ranges`를
  보존해야 한다. 복합 범위의 null을 산술에 넣으면 잘못된 1쪽으로 표시될 수 있다.
- `FE/src/components/home/TaskCard.jsx`와 `FE/src/pages/study/S12Study.jsx`는 현재
  최상위 `from/to`만 표시한다. 단일 범위만 기존 형식으로 표시하고, 복합 범위는 자료별
  `ranges`를 사용해야 한다. `doneTo:null`을 완료 페이지 숫자로 치환하지 않는다.
- `FE/src/api/adapters/study.js::toSession`은 `task`를 그대로 전달하므로 집중의 새 필드는
  전달된다. 화면에서 `task.donePages`, `task.doneTo`, `task.unitNo` 및 `ranges`를 사용할 수 있다.
- `FE/src/api/adapters/today.js::toFixResult`는 R-02 응답의 `ranges/donePages`를 버린다.
  정정 후 범위 상태도 이 응답이나 홈 재조회로 갱신해야 한다. FE 파일은 이번 작업에서 수정하지 않았다.

## 후속 작업의 정확한 경계

- **D-01:** `Task.pages/done_pages` 입력은 위 공통 계산으로 정규화했다. 전체 rate의 HALF_UP,
  출제 가능 분모, 과목의 고유 새 범위, 보충 회차, readiness 계산 정책은 이번 변경에 포함하지 않는다.
- **L-01:** `app/routers/today.py::_notice`가 반환하는 carry `notice.items`에는 아직 `ranges`가 없다.
  생산자인 `app/services/planner.py::_carry`가 저장한 항목은
  `subjectId/from/to/pages/dailyAvg/ratio/date`뿐이다. `taskId/rangeId/materialId/unitId/conceptId`가
  없어 동일 과목의 여러 배치를 정확히 연결할 수 없다. 저장 근거 보완과 해당 조회 연결은 L-01이며,
  이번 조회에서 추정 식별자를 붙이지 않았다.
- **L-04:** `app/routers/today.py::get_today`의 `paceAlert`는 현재 null이다. 실제 제안 생성 시
  `paceAlert.rows[].ranges/pages`를 포함하는 책임은 L-04에 남는다.
- 현재 `plans.approved_at`는 NOT NULL이므로 plans 행은 승인된 계획이다. 계획이 없을 때
  홈의 기존 `404 NO_PLAN` 경계는 유지한다. 별도 미승인 계획 상태를 만들거나 스키마를 바꾸지 않았다.

DB 스키마/마이그레이션, 기록 쓰기, 퀴즈, AI, 전역 집계 정책, `app/main.py`는 변경하지 않았다.

## 검증

테스트는 `tests/conftest.py`가 만드는 SQLite DB와 PostgreSQL의 독립 스키마에서 실행한다.
`TEST_POSTGRES_URL`은 로컬 54511의 `team10_test`를 사용하며 공유 패키지를 설치/수정하지 않았다.

- 공통 모델 독립 커밋: `tests/test_task_views.py` 순수 계산 19 passed.
- 홈 연결 커밋: task_views/record_ranges/record_transactions/record_sessions/today_suggestions/demo_package,
  SQLite+PostgreSQL 282 passed.
- 집중 연결 커밋: task_views/focus_api/focus_state/focus_view/focus_concurrency/demo_focus_seed/demo_focus_expiry/record_sessions,
  SQLite+PostgreSQL 157 passed.

최종 영향 범위 명령:

```sh
TEST_POSTGRES_URL="$LOCAL_TEST_POSTGRES_URL" .venv/bin/python -m pytest -q \
  tests/test_task_views.py tests/test_record_ranges.py tests/test_record_transactions.py \
  tests/test_record_sessions.py tests/test_today_suggestions.py tests/test_demo_package.py \
  tests/test_focus_api.py tests/test_focus_state.py tests/test_focus_view.py \
  tests/test_focus_concurrency.py tests/test_demo_focus_seed.py tests/test_demo_focus_expiry.py \
  tests/test_demo_next_day.py tests/test_planner_exam_boundary.py --tb=short
```

최종 결과: **424 passed**, 107.34초. 기존 SWIG deprecation 경고 5개 외 실패/skip은 없다.
