# SYS-04 미리보기 저장소 인계

`previewId`는 기존 `plan_proposals.id`다. `0007_preview_storage`는
`0006_quiz_answer_storage` 위에 `plan_proposal_states` 한 표를 추가한다.
기존 승인 계획·요청·후보·대화 행은 그대로 유지하고 기존 `plan_proposals.changes`를
실제 변경안의 불변 스냅샷으로 사용한다. 현재 planner/API를 이 저장소에 연결하지 않았다.

## 새 후보의 저장 계약

`ProposalRepository.create`는 기존 요청에 후보를 붙이거나 ready 요청과 후보를 함께 저장한다.
기존 요청을 사용할 때는 owner/kind/base plan ID·revision이 일치하고 ready이며 선택 전이어야 한다.
승인 계획/목표/일정/태스크/plan revision은 생성으로 변경되지 않는다.

| 저장값 | 의미 |
|---|---|
| proposal_id/user_id/kind/target | 기존 후보 ID, 원 request와 일치하는 소유자·종류, 도메인이 제공한 대상 객체 |
| normalized_input/input_hash | 정규화된 JSON 객체와 canonical JSON SHA256 |
| base_date | 후보 생성 판단의 사용자 기준 날짜 |
| base_plan_id/base_plan_revision | 계획 기반 후보의 명시적 계획 ID·revision |
| base_structure_hash | 계획 없는 후보의 자료/목표/일정 구조 해시; 계획 ID·revision과 택일 |
| dependency_versions | 참조 자료/일정/기록/정책 등의 ID·버전 객체 |
| preview_response/preview_schema_version | 생성 시 최초 후보 응답과 양의 정수 형식 버전 |
| status/state_revision | ready/applied/cancelled/stale; 최초0, 상태 전이마다1 증가 |
| result_response/result_schema_version/result_captured_at | 최초 적용 응답과 형식 버전·시각; 적용 전 SQL NULL |
| decided_at | 상태 전이 시각. 최초 성공 재조회 때 바꾸지 않음 |

입력 구조와 의존 버전은 도메인이 제공한다. 예를 들어 최초 구조는 자료/목표/일정의
ID·updated_at과 의미 있는 필드를 포함하고, 집합인 배열은 도메인에서 정렬해야 한다.
저장소가 현재 테이블을 추측해 해시하거나 임의로 배열 순서/문자열 공백을 바꾸지 않는다.
대상 객체의 자료·일정 등 도메인 소유권도 호출 도메인이 검증한다.

## JSON 경계

`canonical_object`는 dict 또는 원문 JSON object를 받아 방어적으로 복사한다.
`canonical_hash`는 이를 UTF-8, 정렬된 객체 키, 공백 없는 구분자로 직렬화해 SHA256을 만든다.
문자열 내용과 배열 순서는 유지한다. 날짜/UUID 등은 도메인이 명시적으로 문자열로 정규화한다.

- NaN/Infinity, 비문자열 객체 키, 순환, 잘못된 surrogate, NUL을 거절한다. NUL은 값과 키 모두 해당한다.
- 원문 JSON은 중복키를 탐지한다. Unicode escape로 표현만 다른 중복키도 거절한다.
- **이미 dict로 파싱하며 사라진 중복키는 복원할 수 없다.** SYS-05의 원문 수신 경계에서도
  이 parser를 사용하거나 같은 중복키 검사를 해야 한다. 이미 손실된 입력을 검증했다고 주장하지 않는다.
- 입력/target/changes/의존 버전/preview 응답/적용 결과 모두 같은 검증을 거친다.
  잘못된 JSON은 사용자 잠금을 포함한 SQL 이전에 `InvalidProposalJSON(ValueError)`로 거절한다.
- SQLite는 NUL을 저장할 수 있지만 PostgreSQL JSONB는 거절하므로 이 공통 경계가 필요하다.
  미통합 AI gateway에 의존하지 않는다.

## 저장소 메서드와 SYS-05 트랜잭션

- `create(...)`: 위 필수 기준·후보 원 응답을 저장한다. `request_id` 생략 시 ready 요청을 만든다.
- `get(user_id, proposal_id)`: 저장된 후보/상태/원 응답/변경안을 조회한다.
- `ready(user_id, kind=..., input_hash=...)`: 아직 선택되지 않은 ready 요청의 ready 후보를 조회한다.
  날짜/의존 버전 freshness나 임의 시간 만료는 여기서 판정하지 않는다.
- `mark_applied(..., expected_revision, response, now, schema_version=1)`: request의
  ready→accepted 선택 CAS와 후보의 ready→applied CAS를 한 savepoint에 묶는다.
  같은 request의 다른 후보가 이미 선택됐으면 실패한다. 같은 후보 재호출은 최초 결과·시각·버전을 반환한다.
- `cancel`/`mark_stale`: ready 후보만 종결하며 request의 다른 후보를 임의로 바꾸지 않는다.
  취소/stale 후보의 재적용·다른 종결 상태로 전이는 거절한다.
- `result(...)`: 적용된 최초 응답을 반환한다.

상태 전이는 `ProposalTransition(proposal, changed)`를 반환한다. 최초 전이는 changed=true,
동일 applied 후보 재생은 false다. 재생 때 새로 받은 응답으로 원 응답을 덮어쓰지 않는다.
저장소는 commit하지 않는다. 생성 실패나 상태 CAS 실패는 내부 savepoint를 되돌린다.
사용자 잠금은 savepoint보다 먼저 잡아 SQLite에서 savepoint release가 외부 트랜잭션을
뜻하지 않게 확정하는 문제를 피한다.

**SYS-05는 같은 caller 트랜잭션에서 사용자 잠금 → 후보 조회 → 실제 날짜/의존 버전 비교 →
저장 changes 적용 → plan revision 증가 → mark_applied 순서로 처리해야 한다.**
도메인 실행 실패/CAS 충돌 예외를 삼켜 commit하지 말고 전체 caller 트랜잭션을 rollback한다.
별도 요청의 두 후보 경쟁과 현재 날짜/자료/일정/기록 변화 판정은 SYS-05의 책임이다.
이 저장소 primitive만 호출했다고 실제 도메인 적용이 완료된 것은 아니다.

## 과거 이력과 예외

0007은 원 request/proposal의 알려진 사실을 `legacy_snapshot`에 남긴다.
request.status=accepted이고 accepted_proposal_id가 해당 후보일 때만 legacy applied로 보존한다.
그 외는 legacy 상태이며, 기존 rejected/stale/needs_input 등의 의미는 원 행과 snapshot에 그대로 남는다.
effective_date를 base_date로 바꾸거나 현재 자료 버전을 과거 버전으로 추정하지 않는다.
알 수 없는 normalized input/hash/대상/의존 버전/preview 응답/적용 응답은 SQL NULL이다.
확인되는 기존 base plan ID·revision 및 적용 결정 시각은 그대로 보존한다.

| 결과/예외 | SYS-05가 구분할 경계 |
|---|---|
| ProposalNotFound(foreign=False) | 없는 후보/요청. 공개 NO_PREVIEW 경계로 매핑 |
| ProposalNotFound(foreign=True) | 다른 소유자의 후보/요청. 공개404 NO_PREVIEW 경계 |
| ProposalConflict | 요청 owner/kind/basis 불일치, 상태 revision 충돌, 이미 다른 후보가 선택됨 |
| LegacyProposalUnavailable | 기준이 불완전한 legacy 또는 옛 writer가 상태 없이 만든 후보; 새 후보가 필요함 |
| LegacyResponseUnavailable | 과거 적용은 확인되지만 최초 응답은 없음; 재실행/임의 응답 생성 금지 |
| UserBusyError | 공통 사용자 잠금 경합; 서비스에서409 REQUEST_IN_PROGRESS로 매핑 |
| ValueError / InvalidProposalJSON | 형식 오류. SQL 이전 검증 또는 결정 시각/버전 오류 |

LegacyResponseUnavailable은 LegacyProposalUnavailable의 하위 예외다. 먼저 구분해서 처리한다.
legacy의 null 응답을 재실행 허가로 바꾸지 않으며, SYS-05가 재조회 호환 정책을 별도로 정해야 한다.

## migration·삭제 경계

proposal_id FK에만 CASCADE를 둔다. 기존 `app/seed/loader.py::wipe_user`는 요청의 순환 참조를
해제한 뒤 proposal을 삭제하므로 자식 상태도 안전하게 삭제된다. 다른 사용자 후보·계획·이력은
유지되는 것을 두 DB에서 검증했다. 사용자/자료/원본 질문을 삭제하는 cascade는 추가하지 않았다.
downgrade는 새 상태 표만 제거하며 원 request/proposal/승인·대화 행은 보존한다.
SQL-only migration 출력은 DDL만 제공한다. legacy 이관에는 online upgrade가 필요하다.

## 검증

- 생성기 병합·중복 구성 거절 테스트 통과. 기존 schema 생성 결과도 유지한다.
- 스키마·0006 upgrade/downgrade·legacy/null·실제 reset 순서·기존 migration: SQLite+PG 88 passed.
- 생성/조회·rollback·소유권·SQL 이전 JSON 검증: SQLite+PG 및 순수 JSON 76 passed.
- 전이·동시 선택·최초 결과 재생·caller rollback·legacy 예외까지: 110 passed.

최종 검증은 별도 SQLite DB와 PostgreSQL 독립 schema를 사용하며 기존 배포 DB를 변경하지 않는다.

```sh
TEST_POSTGRES_URL="$LOCAL_TEST_POSTGRES_URL" .venv/bin/python -m pytest -q \
  tests/test_proposal_storage.py tests/test_proposal_storage_json.py \
  tests/test_proposal_storage_transitions.py tests/test_proposal_storage_migration.py \
  tests/test_migrations.py tests/test_quiz_storage_migration.py \
  tests/test_demo_state.py tests/test_demo_seed_migration.py --tb=short
```

로컬 54511의 team10_test에서 **222 passed**, 63.65초. 기존 SWIG deprecation 경고5개 외 실패/skip 없음.
`NODE_PATH`로 원본 BE의 generator 의존성을 읽기 전용 사용해 `test_gen_schema.js`가 통과했고,
`gen_schema.js` 재생성 후 `git diff --exit-code -- app/db/schema.py`도 통과했다.
생성 결과43 tables/84 FKs, Alembic 단일 head는 `0007_preview_storage`다.
