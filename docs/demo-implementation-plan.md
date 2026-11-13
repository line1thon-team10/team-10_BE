# 데모 계약 구현 계획

기준: PR #7 `feat/session-domain` 위의 `feat/demo-contract`. 명세는 `docs/api/session.md`, `common.md`, `manage.md`다. 기존 배포 브랜치를 수정하지 않는다.

1. 기존 migration 테스트에서 불변 baseline 검사와 현재 head 검사를 구분한다. 테스트 보정만 먼저 커밋한다.
2. `users.demo_seed`의 nullable 컬럼을 ERD와 새 migration에 추가하고 도메인·시드 적재를 연결한다. 기존 선택값을 날짜로 역추정하지 않는다. 기존 사용자는 null을 유지하며 이후 seed/reset에서 실제 값을 기록한다.
3. 데모 저장소·서비스·HTTP 모델을 연결한다. firstExam은 오늘 이후 중 날짜/ID 순으로 선택하고 없으면 null이다. seed/reset은 사용자 잠금 아래 원자적으로 처리한다. 데모 비활성, 잘못된 입력, 다른 사용자 데이터 보존, 중간 실패 rollback을 검증한다.
4. next-day에서 같은 사용자 mutation을 잠금으로 보호한다. 빈 계획은 400 NO_PLAN, 마지막 시험일 이후로 이동은 400 DEMO_END. 시험일 다음 날 보관하며 시험 당일 이후로 새 이월 태스크를 만들지 않는다. 기존 기록은 보존한다.
5. next-day 전용 재시도 응답 테이블을 별도 migration으로 추가한다. UUID Idempotency-Key의 성공 응답을 같은 트랜잭션에 저장하고, 동일 요청은 저장된 응답을 반환한다. 동시 진행 중이면 409 REQUEST_IN_PROGRESS, 키 없으면 별도 하루 이동이다. seed/reset도 같은 사용자 잠금을 사용하고 재시도 기록을 임의 삭제하지 않는다.
6. SQLite·실제 PostgreSQL에서 전체 회귀 및 동시성 검증 후 PR을 생성한다. 명세의 모호한 경계와 FE/기존 BE의 차이, migration 영향, 아직 남은 기능을 PR에 명시한다.

각 구현과 이를 검증하는 테스트는 같은 원자 커밋에 넣는다. 계획 엔진의 다른 계산식/재분배 기능과 선택적 `/api/demo/ai`는 이번 범위에 포함하지 않는다. 원문과 충돌하는 새 계산 정책을 임의로 확정하지 않는다.
