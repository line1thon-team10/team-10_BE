# DB 마이그레이션 운영·개발

스키마 변경은 Alembic revision으로 관리한다. 서버 시작은 현재 DB의 revision이 코드의 단일 head와 같은지만 읽어서 확인하며, `create_all()`이나 자동 upgrade를 실행하지 않는다. 배포 순서는 **DB 백업 → 마이그레이션 단일 실행 → 서버 worker 시작**이다.

## 데모 계약 revision

- `0002_demo_seed`: `users.demo_seed` nullable 컬럼을 추가한다. 기존 사용자의 이력을 날짜로 추정하거나 seed를 다시 적재하지 않는다. 기존 값은 null, 이후 실제 seed/reset에서 선택값을 기록한다.
- `0003_demo_day_requests`: next-day의 사용자별 재시도 키·요청 해시·성공 응답·생성 시각을 저장한다. 경로/메서드는 next-day로 고정하고 완료된 200 응답만 보관한다. 일반적인 전체 API 재시도 저장소가 아니다.

기존 baseline DB도 백업 후 `python -m alembic upgrade head`로 순서대로 올린다. 현재 모델을 직접 `create_all()`하거나 기존 DB에 baseline을 다시 stamp하지 않는다. 테스트는 fresh upgrade, baseline 사용자 데이터 보존, 각 revision downgrade/reupgrade와 PostgreSQL 스키마 일치를 확인한다.

재시도 기록은 자동 만료하지 않고 seed/reset에서도 보존한다. 용량 관리는 후속 운영 정책이며 임의 정리는 하지 않는다. `0003` downgrade는 응답 기록을, `0002` downgrade는 저장한 seed 선택값을 잃는다. 운영에서 schema downgrade만 실행하면 중복 방지 이력이 사라질 수 있으므로 코드를 함께 검토하고 현재 DB를 먼저 백업해야 한다. 이 PR은 운영 DB에 migration을 실행하지 않는다.

PostgreSQL mutation은 사용자 행의 NOWAIT 잠금으로 직렬화한다. SQLite는 DB 단위 쓰기 잠금을 사용하므로 다른 사용자의 쓰기도 잠시 충돌할 수 있다. 잠금 충돌은 409로 반환하며 트랜잭션이 끝날 때 해제된다.

## 이번 baseline 범위

- `0001_baseline`은 기존 생성형 SQLAlchemy 스키마의36개 테이블을 그대로 고정한 초기 revision이다. API 도메인이나 새로운 ERD 제약을 추가하지 않는다.
- `migrations/baselines/schema_0001.py`는 과거 스키마 스냅샷이다. 이후 ERD/모델이 바뀌어도 수정하거나 재생성하지 않는다. 초기 revision에서 현재 `app.db.schema`를 import하면 과거 migration 의미가 달라지므로 금지한다.
- 각 upgrade/downgrade는 스냅샷을 복제해서 실행한다. PostgreSQL 순환 외래키 ALTER의 내부 실행 상태가 후속 SQLite 생성에 남지 않게 한다.
- 부분 유일 인덱스·점수/페이지 CHECK 등 명세의 추가 제약은 각 도메인 구현 PR에서 별도 revision으로 적용한다. 이번 baseline이 최신 API 명세 전체를 구현했다는 뜻은 아니다.

## 신규 DB

BE 루트에서 현재 가상환경에 requirements.txt를 설치한 뒤 실행한다. Windows는 `.venv/Scripts/python`을 사용한다.

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

`DATABASE_URL`은 app.config/.env와 동일하게 읽는다. 값이 없거나 빈 문자열/공백이면 BE 루트의 `pirochim.db`를 사용한다. PostgreSQL URL도 환경변수로 설정하며 alembic.ini에는 비밀번호를 저장하지 않는다. URL은 ConfigParser에 다시 넣지 않아 비밀번호의 `%`를 보존한다.

`upgrade head`는 같은 revision에 재실행해도 데이터를 초기화하지 않는다. 기존 테이블이 있지만 revision이 없는 DB에는 초기 baseline을 생성하지 않고 아래 인계 절차를 안내한다.

## 기존 create_all DB 인계

서버와 쓰기 작업을 중지하고 백업한 DB 사본에서 먼저 검증한다. 실제 DB에 적용할 때도 같은 순서로 실행한다.

```bash
.venv/bin/python -m app.db.migrations adopt-baseline
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

인계 명령은 테이블/뷰, 컬럼 구성·타입·길이·NULL/default, PK/FK, 인덱스·유일 제약을 baseline과 비교한다. baseline에 없는 CHECK/부분 인덱스도 거절한다. 관리 대상 구조가 일치할 때만 `alembic_version=0001_baseline`을 기록하며 기존 행을 UPDATE/DELETE하지 않는다. 이미 baseline이면 검증 후 동일 상태를 유지한다. 빈 DB, 일부 테이블만 있는 DB, 다른/알 수 없는 revision은 등록하지 않는다.

검증 실패 시 임의로 `alembic stamp head`를 실행하지 않는다. 오류가 난 DB와 baseline 차이를 확인하고, 실제 상태에 맞는 이행 revision을 별도로 작성한다. DB 계정/권한, trigger/function, 확장 모듈 등 SQLAlchemy metadata 밖의 객체까지 동일하다고 인증하는 명령은 아니다. 이 명령은 서버 쓰기가 중지된 유지보수 상태에서 사용한다.

## 이후 도메인별 변경

1. 해당 stacked 브랜치에서 ERD 수정 및 `scripts/gen_schema/gen_schema.js` 재실행으로 현재 모델을 갱신한다.
2. 직전 revision까지 적용한 **개발 DB**에서 후보 revision을 생성한다.
3. 생성 결과를 리뷰해 데이터 이행, 제약 이름, 순환 참조, SQLite batch 처리와 PostgreSQL 동작을 확인한다. 자동 생성이 rename/모든 CHECK/데이터 변환을 완성해 주지는 않는다.
4. 기존 데이터가 있는 DB에서 upgrade·재실행·실패/rollback을 검증한다. 지원하는 downgrade의 데이터 영향도 명시한다.
5. ERD/현재 모델/revision/해당 테스트를 같은 도메인의 원자 커밋에 포함한다. 서로 다른 도메인의 변경이나 공통 설정 변경을 섞지 않는다.

```bash
.venv/bin/python -m alembic revision --autogenerate -m "subject example"
# 생성된 upgrade/downgrade를 직접 검토한 뒤 실행
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic check
```

자동 생성 후보는 반드시 사람이 검토한다. [Alembic 공식 autogenerate 설명](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)도 자동 탐지의 한계를 명시한다.

스택에서는 부모 PR의 head revision을 새 revision의 down_revision으로 삼는다. 코드의 head가 여러 개면 서버 시작을 거절한다. 부모 PR의 revision이 바뀌면 자식의 의존성과 테스트 DB를 함께 점검한다. 이미 배포한 revision을 고쳐 기존 이력을 바꾸지 않는다.

## 되돌리기와 SQL 확인

초기 `downgrade base`는36개 테이블을 삭제하는 작업이다. 이번 검증은 빈 임시 DB의 downgrade/re-upgrade를 대상으로 했으며, 운영 데이터 복구는 백업을 기준으로 한다. 특히 SQLite DDL은 PostgreSQL처럼 모든 실패가 한 트랜잭션으로 되돌아간다고 가정하지 않는다.

```bash
# 적용하지 않고 검토용 SQL 출력
.venv/bin/python -m alembic upgrade head --sql
```

offline SQL 생성은 DB를 조회할 수 없으므로 기존 DB 구조 검증을 대신하지 않는다. 생성 SQL을 운영 DB에 바로 실행하는 대신 위 online 절차로 적용한다.

## 테스트

```bash
.venv/bin/python -m pytest -q tests
# 별도의 테스트 전용 PostgreSQL DB 연결 문자열을 지정
TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:5432/team10_test \
  .venv/bin/python -m pytest -q tests
```

TEST_POSTGRES_URL이 없으면 SQLite 검증만 실행한다. 지정하면 마이그레이션 테스트를 양쪽 DB에서 실행한다. PostgreSQL 테스트는 매번 임의의 `test_<uuid>` 스키마를 생성하고 종료 시 해당 스키마만 삭제하므로 스키마 생성 권한이 필요하다. 실제 서비스 DB를 테스트 URL로 쓰지 않는다.

커버하는 경계: 신규 DB, 재실행 데이터 보존, 빈 baseline downgrade/re-upgrade, 기존 DB 인계/재인계, 누락 테이블·컬럼·키·인덱스·타입/길이·NULL/default 차이, CHECK/부분 인덱스/뷰 추가, unknown/multiple revision, 인계 rollback, 읽기 전용 startup guard, PostgreSQL SQL 생성 뒤 SQLite 순환 FK 보존.
