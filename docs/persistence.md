# 도메인과 저장소

`app/db/schema.py`의 SQLAlchemy Core Table이 도메인 영속 모델이다. 루트에서 제공한 ERD와 `schema/erd.dbml`은 동일하며 중복 ORM 모델을 추가하지 않는다. `app/repositories/`는 모든 ERD 테이블에 명시적인 저장소 클래스를 제공한다.

저장소는 요청의 Connection을 받아 사용하며 commit/rollback을 호출하지 않는다. 트랜잭션 경계는 `get_conn`이다. 복합 PK는 모든 키가 필요하고 PK 수정·빈 수정·잘못된 페이지 범위는 거절한다. 조회는 PK 순으로 정렬한다.

저장소는 내부 영속 계층이며 인증 경계가 아니다. 서비스는 사용자 소유권 조건을 조회/수정/삭제에 전달하거나 소유권을 검증한 aggregate의 ID만 사용해야 한다. 연관 대상의 사용자 일치도 서비스에서 확인해야 한다. 임의 외부 입력을 values로 직접 넘기지 않는다.

익명 세션 생성·조회에는 UsersRepository를 연결했다. 기존 학습 서비스의 복합 조회는 Core SQL을 유지한다. 도메인별 저장소 도입과 모든 기존 쿼리의 재작성은 구별한다.

ERD 노트의 부분 유일 인덱스 원본 `erd-constraints.sql`은 제공되지 않았다. 현재 테이블 정의가 모든 비즈니스 제약을 DB에서 보장하지는 않는다. 마이그레이션과 PostgreSQL 통합 검증은 후속 작업이다.

검증: `.venv/bin/python -m pytest -q` (pytest, httpx 별도 설치). SQLite에서 CRUD, 복합 키 누락, 소유권 조건, 페이지 경계, 롤백, 잘못된 익명 UUID, 채점 실패를 검증한다.
