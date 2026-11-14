# 런타임 계약 검증

문서 목록, 실제 라우트 등록, HTTP 동작을 각각 확인한다. 경로가 등록됐다는 사실만으로 응답 필드나 업무 규칙이 완성됐다고 표시하지 않는다.

```sh
.venv/bin/python scripts/check_api_spec.py --fe ../FE
.venv/bin/python scripts/check_api_spec.py --runtime
.venv/bin/python -m pytest -q tests/test_contract_inventory.py tests/contract_cases
```

`--runtime --require-complete`는 필수 경로가 하나라도 미등록이면 실패하는 최종 릴리스 검사다. 개발 중인 미등록 경로는 `required_pending`, 선택 경로는 `optional_pending`에 따로 남는다. 알려지지 않은 API와 경로 파라미터 이름만 다른 중복 라우트도 실패시킨다. 서버 생성을 넘어 운영 서버에 접속하거나 사용자 데이터를 변경하지 않는다.

HTTP 공통 검사는 등록된 모든 보호 API에 세션 누락/잘못된 세션을 보내401, 공통 오류 envelope, `Cache-Control: no-store`, DB 쓰기 부재를 확인한다. 소유권 검사는 실제 다른 사용자 객체로 수행한다. 잘못된 ID의 응답은 각 도메인 계약을 따른다. 온보딩 draft 조회의 잘못된 컨텍스트는404이며 퀴즈/집중의 엄격한 UUID 입력은422다.

현재 업무별 실제 응답과 변경 검사는 다음 테스트에 연결한다. 이 표는 후속 기능 전체의 완료 선언이 아니다.

| 범위 | 실제 요청/응답 검증 | 주요 경계 |
|---|---|---|
| 익명 세션·데모 | [session](../test_session_api.py), [demo quiz](../test_demo_quiz_flow.py) | 빈 계정/시드, 사용자 격리, 데모 제한 |
| 온보딩 조회 O-01 | [onboarding API](../test_onboarding_api.py) | 확인 필드, null, draft 컨텍스트, 타인 객체, 조회 부작용 |
| 퀴즈 식별 Q-01 | [quiz identifiers](../test_quiz_identifiers.py) | 공개 item ID, 고정 quizId, 출처 비노출, 잘못된 답안 원자 거절 |
| 기록 R-02 | [record ranges](../test_record_ranges.py), [transactions](../test_record_transactions.py) | 복합 범위, 전체 교체,0쪽 정정, 이월 상태 보존, 롤백 |
| 집중 | [focus API](../test_focus_api.py), [concurrency](../test_focus_concurrency.py) | 일시정지/재개/종료, 타인 세션, 경쟁 요청 |

각 도메인 작업은 이 표의 기존 검증을 확장하거나 새 테스트를 연결한다. 정상 응답의 정확한 필드, 빈 값, 외래 객체, 거절 요청의 부작용, 재시도/경쟁은 해당 도메인 테스트에서 확인한다. 공통 검사가 통과하더라도 `required_pending`의 기능을 구현된 것으로 처리하지 않는다. SQLite와 PostgreSQL 모두에서 검증하려면 `TEST_POSTGRES_URL`을 격리 테스트 DB로 설정한다.
