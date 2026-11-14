# SYS-05 공통 미리보기 승인 서비스

`app/services/proposals.py`는 SYS-04 저장소 위에 후보 생성·승인·취소의 공통 트랜잭션을
구현한다. 기존 planner/router를 교체하거나 연결하지 않았으며 스키마·migration·생성기·
퀴즈·AI·인프라·`app/main.py`는 수정하지 않았다. 기존 repository는 아래 old writer의
알려진 적용 사실을 같은 joined snapshot에서 구분하는 최소 보완만 수행했다.

## 서버 도메인 인터페이스

`PreviewDomain(kind, plan_effect, inspect_basis, apply_changes, result_schema_version=1)`은
서버 코드가 등록하는 설정이다. 요청 body로 kind/plan_effect/콜백을 선택하지 않는다.
같은 후보의 호출 경로는 서버가 기대하는 kind와 정규화한 target을 명시한다.

- `inspect_basis(connection, context, target) -> DomainBasis`: 현재 관련 자료·목표·일정·
  기록·정책 등의 ID/버전을 읽는다. 계획이 없으면 최초 구조의 canonical SHA256도
  반환한다. 대상 리소스의 소유권 확인과 집합/대상 UUID 등의 도메인 정규화는 이
  도메인의 책임이다. 공통 서비스가 도메인 테이블이나 구조 해시를 추측하지 않는다.
- `DomainBasis(dependency_versions, structure_hash=None)`: JSON 버전 객체를 불변 문자열로
  보관한다. dependency_versions 접근은 매번 새 복사본을 반환한다. 계획이 있으면
  structure_hash는 None, 없으면 실제 현재 구조의 해시가 필수다.
- `apply_changes(connection, context, saved_changes) -> dict | JSON object 문자열`:
  **저장된 changes의 방어복사**만 받는다. 적용 요청에는 새 changes를 받는 서비스
  인자가 없다. 적용 결과의 HTTP data 객체를 반환하며 공통 서비스가 최초 값을 저장한다.
- `PreviewContext`: 잠금 뒤 재조회한 frozen User, 한 번 캡처한 UTC now, 그 시각과
  최신 timezone/demo_date로 계산한 today, 현재 서비스 소유 PlanState를 제공한다.
  콜백은 시각을 다시 잡지 않고 context.now/today를 사용한다.

콜백은 동기식의 제한된 로컬 DB 작업이다. inspect_basis는 읽기 전용이고 apply_changes는
검증된 도메인 변경만 기록한다. 외부 AI/네트워크/긴 계산, commit/rollback, 계획 헤더와
후보/요청 메타데이터 직접 변경은 콜백의 책임 범위가 아니다. 도메인 삭제/수정으로
참조 데이터가 사라지면 변경된 버전/구조(예: 삭제 표시)를 반환하거나 ProposalConflict를
발생시켜 PREVIEW_STALE로 처리한다. 개별 도메인은 승인 시 의존 버전도 실제 변경과 함께
갱신해야 한다. 특히 계획 없는 draft에는 plans.revision을 대신 만들어 주지 않으므로
그 draft의 구조/의존 버전이 경쟁 후보를 무효화할 근거다.

## 공개 서비스 메서드

| 메서드 | 결과와 책임 |
| --- | --- |
| `capture(user_id, domain=..., target=...)` | 일관된 불변 PreviewSnapshot. 날짜·계획 기준·도메인 버전을 사용자 잠금 아래 캡처 |
| `create(user_id, domain=..., target=..., snapshot=..., normalized_input=..., changes=..., preview_response=..., proposal_type=..., description=..., ...)` | 준비 당시 기준과 현재 기준을 다시 비교한 뒤 후보만 저장. 원 preview 응답에 실제 previewId를 보존해 반환 |
| `apply(user_id, preview_id, domain=..., target=...)` | 저장 changes를 한 번 적용하고 최초 결과 data를 반환. applied 재시도는 원 응답만 재생 |
| `cancel(user_id, preview_id, domain=..., target=...)` | 후보만 취소하고 기존 외부 계약 `{cancelled:true}` 반환 |

create의 선택 인자는 SYS-04의 request_id/request_text, 내부 preview_id, preview_schema_version이다.
preview_id를 생략하면 서버가 UUID를 생성한다. 이미 응답에 previewId가 있으면 실제 ID와
일치해야 한다. 같은 request의 형제 후보는 request_id를 명시한다. create는 apply_changes를
호출하지 않으며 승인 계획·목표·일정·기록·revision을 변경하지 않는다.

PreviewSnapshot의 owner/kind/target과 DomainBasis의 JSON은 불변이다. 반환한 target/버전,
준비 입력, 콜백에 넘긴 changes, 반환 응답을 호출자가 수정해도 저장 snapshot은 변하지 않는다.
JSON의 문자열 공백·배열 순서는 유지하며 숫자 표현과 불리언 구분은 SYS-04 canonical
검증을 재사용한다. NaN/Infinity/NUL/잘못된 Unicode/중복키/순환 등은 허용하지 않는다.
이미 dict를 만들며 소실한 중복키는 복구할 수 없으므로 원문 JSON 수신 경계도 같은
parser를 사용해야 한다. 날짜/UUID는 도메인이 JSON 문자열로 명시적으로 변환한다.

## 긴 준비와 caller 트랜잭션

서비스는 commit하지 않는다. 사용자 lock을 내부 savepoint보다 먼저 잡는다.
SQLite에서도 savepoint release가 외부 트랜잭션을 실수로 commit하지 않으며,
호출자가 성공 후 rollback하면 도메인·계획·후보 성공 상태가 함께 되돌아간다.

호출 구조는 다음과 같다. `prepare_verified_preview` 등은 해당 도메인의 실제 구현이다.

```python
with engine.begin() as conn:
    basis = ProposalService(conn).capture(user_id, domain=operation, target=target)

# 사용자 lock과 DB 트랜잭션이 끝난 뒤 AI/배분 계산/출력 검증을 수행한다.
prepared = prepare_verified_preview(basis, normalized_input)

with engine.begin() as conn:
    preview = ProposalService(conn).create(
        user_id, domain=operation, target=target, snapshot=basis,
        normalized_input=normalized_input, changes=prepared.changes,
        preview_response=prepared.response, proposal_type=prepared.type,
        description=prepared.description,
    )

# 별도 승인 요청: body에서는 previewId만 사용한다.
with engine.begin() as conn:
    result = ProposalService(conn).apply(
        user_id, body.previewId, domain=operation, target=target,
    )
```

준비 중 날짜·계획·자료 등이 바뀌면 create도409 PREVIEW_STALE이고 부분 후보를 남기지 않는다.
외부 AI 실패를 매핑하는 책임은 도메인/AI gateway에 남으며, 공통 서비스가 실패를 성공
후보로 대체하지 않는다. 내부 savepoint는 콜백·SQL·직렬화·plan CAS·후보 CAS 실패를 모두
되돌린다. 기존 요청별 get_conn도 예외를 전파받아 caller 트랜잭션을 rollback해야 한다.
특히 PostgreSQL 잠금 경합 뒤에는 caller rollback이 필요하다.

## 승인 순서와 revision 소유권

1. 사용자 잠금 → 최신 User 재조회 → UTC 시각 한 번 캡처 → timezone/demo_date의 today.
2. 명시한 후보를 조회하고 owner/kind/target을 확인한다.
3. 새 형식의 applied이면 현재 날짜·계획·도메인 버전을 조회하기 전에 최초 응답을 재생한다.
4. ready 후보의 날짜와 계획 ID/revision을 먼저 검사하고, 도메인의 현재 구조·의존 버전을
   비교한다. 이미 달라진 공통 기준은 도메인 조회 오류로 가리지 않고409로 반환한다.
5. 저장된 changes를 적용하고 아래 계획 규칙을 검증한다. 변경·revision·request 선택
   CAS·후보 applied와 최초 result/captured_at을 같은 savepoint 안에서 완료한다.

| 서버 plan_effect | 계획 헤더와 revision |
| --- | --- |
| `PlanEffect.UPDATE` | 기존 계획이 필수. 콜백에는 적용 전 PlanState를 전달하며 헤더를 그대로 유지해야 한다. 콜백 후 서비스가 해당 owner/plan ID/원 revision을 CAS하여 정확히 +1 |
| `PlanEffect.CREATE` | 계획이 없어야 한다. 사용자 잠금 뒤 savepoint 안에서 revision1의 헤더를 먼저 만들어 FK용 ID를 콜백에 전달. 끝까지 ID/revision1/생성·승인 시각을 보존하며 실패하면 새 헤더도 제거 |
| `PlanEffect.DRAFT` | 승인 계획이 없어야 하며 콜백 전후 모두 없어야 한다. 실제 draft 변경은 도메인이 수행하고 공통 서비스는 계획 생성/revision 증가를 하지 않음 |

콜백이 plan identity/owner/revision/생성·승인 시각을 바꾸면 검출하고 rollback한다.
같은 request의 다른 후보가 이미 선택됐으면 콜백 전에 거절한다. 별도 request의 후보도
계획 revision 또는 초기 구조/의존 버전 변화로 거절한다. 동일 후보의 병렬 요청은 사용자
잠금 경합이면409 REQUEST_IN_PROGRESS이며 성공 commit 후 재시도는 최초 응답을 반환한다.
초기 계획 생성 경쟁에서도 첫 성공만 revision1 계획을 남긴다.

## 오류·재생·취소 계약

- preview_id=None은409 PREVIEW_REQUIRED. 서비스에서 잘못된 UUID/형식 및 없는/삭제된
  후보는400 NO_PREVIEW다. 정상 문자열의 대문자/하이픈 없는 같은 UUID는 정규화한다.
  향후 HTTP 모델의 일반 요청 타입 검증은 common.md의422 경계를 별도로 지켜야 한다.
- 타인 후보 또는 현재 경로와 kind/target이 다른 후보는404 NO_PREVIEW다. applied라도
  이 확인을 먼저 수행한다. compound target의 ID/집합 정규화는 도메인이 맡는다.
- 날짜/plan ID·revision/초기 구조/참조 버전 변화, cancelled/stale 및 legacy 불완전
  후보는409 PREVIEW_STALE다. 실패를 응답하며 stale 표시를 별도 commit하지 않는다.
- legacy applied는 LegacyResponseUnavailable을 상위 LegacyProposalUnavailable보다 먼저
  구분한다.409와 함께 이미 적용된 이력이나 최초 응답을 복원할 수 없다는 안내를 반환한다.
  과거 응답 생성·재실행·이력 relabel은 하지 않는다. 상태 sidecar 없는 옛 writer도
  request.status=accepted이고 실제 selected proposal ID가 일치할 때만 이 적용 사실을
  구분한다. 미선택 형제/ready는 일반 legacy 불완전 후보로 남는다. 원 proposal ID와
  request.kind를 기존 joined SELECT에 포함하고 known_kind만 예외 메타데이터로 전달해
  다른 종류의 경로는404를 우선한다. unknown target/버전/응답은 추정하지 않으며
  추가 SELECT로 서로 다른 시점의 상태를 섞지 않는다.
- 같은 applied 후보는 owner/kind/target만 맞으면 날짜·계획이 나중에 바뀌어도 원 응답·
  captured_at·decided_at·state_revision을 유지한다. 현재 의존성 콜백도 호출하지 않는다.
- 취소는 ready 후보만 종결하고 request/형제 후보/승인 도메인은 유지한다. ready의 기준
  날짜·계획이 이미 달라도 취소할 수 있다. 동일 취소는 `{cancelled:true}`와 원 decided_at을
  유지한다. 이미 applied/stale/legacy인 후보를 취소 상태로 바꾸지 않는다.
- 임의30분 만료나 최신 후보 자동 선택은 없다. apply/cancel은 항상 명시 ID가 필요하다.
  GET resplit의 기존 무ID 조회 호환(정확히 하나만 허용)은 향후 조회 도메인의 책임이다.
- 잘못된 콜백/직렬화/SQL 오류는 rollback 후 기존 오류 경계로 전파한다. 새 공개 오류
  코드를 만들지 않았다. 사용자 없음/형식 오류는 기존401 UNKNOWN_SESSION이다.

## 남은 연결과 검증

P-02/L-02/U/C/M 계열의 실제 도메인들은 kind/target 정규화, 의미 있는 의존 버전과
초기 구조, 검증된 변경안 생성, 저장 changes 적용을 제공하고 기존 endpoint를 이 서비스로
연결해야 한다. 기존 planner 경로를 공통 서비스에 연결했다고 표시하지 않는다. 처음 계획을
승인하는 후보와 승인 전 draft 교체의 서로 다른 책임도 해당 서버 도메인이 명시한다.
FE는 이미 합의된 previewId 전달·보존과 PREVIEW_STALE/REQUIRED 처리를 따라야 하며
이번 구현으로 새 공개 필드나 응답 종류는 추가하지 않았다.

- capture/create: SQLite+PG42 passed.
- apply/replay/세 revision 모드/rollback/legacy를 포함한 생성·승인:152 passed.
- 후보 전용 취소/재시도/legacy/CAS:22 passed.
- 실제 별도 연결 경쟁:26 passed,2 SQLite-only skips. 두 skip은 PostgreSQL의 독립 행
  writer CAS를 위한 것으로 PostgreSQL에서는 실제 외부 writer를 실행해 통과했다.
- 선행 stale 검사 재현에서 수정 전4 failures를 확인했다. 수정 후 생성·승인·경쟁
  회귀186 passed,2 SQLite-only skips, 기존 SWIG 경고5개였다.
- old writer 보완 전 최종9파일은393 passed,5 SQLite-only skips,104.80초였다.
  이후 accepted+sidecar 없음의 안내 불일치를1 failed/1 passed로 재현했고, 승인된
  저장소 보완 후 승인/취소/저장소/동시 읽기/전이 영향231 passed,3 SQLite-only skips,
  66.34초를 확인했다. 기존393 passed 근거를 대체하거나 미실행 보완을 포함한 결과로
  표시하지 않는다.

최종 검증은 아래 명령으로 SQLite DB와 로컬 PostgreSQL의 독립 schema에서 실행한다.
`LOCAL_TEST_POSTGRES_URL`은 승인된127.0.0.1:54511/team10_test 연결이다.

```sh
TEST_POSTGRES_URL="$LOCAL_TEST_POSTGRES_URL" .venv/bin/python -m pytest -q \
  tests/test_proposals.py tests/test_proposals_apply.py tests/test_proposals_cancel.py \
  tests/test_proposals_concurrency.py tests/test_proposal_storage.py \
  tests/test_proposal_storage_json.py tests/test_proposal_storage_transitions.py \
  tests/test_proposal_storage_reads.py tests/test_proposal_storage_migration.py --tb=short
```

old writer 보완까지 포함한 최종 결과는 **397 passed,5 skipped**,106.72초다.
skip은 기존 동시 읽기3개와 외부 CAS2개의 SQLite 제외이며 PostgreSQL에서는 모두 실행했다.
기존 SWIG deprecation 경고5개 외 실패는 없다. 공유 venv 패키지 변경, 외부 AI 호출,
push/PR/운영 작업은 수행하지 않았다.
