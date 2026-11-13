# session API

## 세션 발급 구현 범위

`feat/session-domain`에서는 `POST /api/session`과 공통 익명 인증을 구현·검증한다. 요청 본문은 생략하거나 빈 객체를 보내며, 정의되지 않은 필드는 `422 INVALID_INPUT`이다. 응답에는 `anonymousId`만 포함하고, 사용자 내부 상태나 다른 사용자 ID를 추가하지 않는다.

- `app/domain/user.py`: UUID v4, Asia/Seoul, draft 및 빈 사용자 상태 생성.
- `app/repositories/users.py`: 사용자 저장·조회. 자체 commit 없이 요청의 트랜잭션에 참여한다.
- `app/services/session.py`: 사용자 생성과 선택적인 day9 시드를 같은 트랜잭션으로 적용한다. 시드 실패 시 생성된 사용자·관련 행을 함께 rollback한다.
- `app/core/auth.py`: UUID를 조회 전에 검증하고 사용자 레포지토리로 인증한다. [공통 계약](common.md)의 중복 헤더 규칙을 따른다.
- `/api/` 응답은 `Cache-Control: no-store`를 적용하며, 처리되지 않은 500 오류도 내부 정보 없이 no-store로 반환한다.

기존 FE mock의 `mock-...` ID는 mock 내부 값이며 실제 BE 자격이 아니다. FE의 실제 연동은 서버가 반환한 UUID를 보관하는 기존 흐름을 유지한다. 기존 BE가 잘못된 UUID를 DB에 직접 조회하던 동작은 `401 UNKNOWN_SESSION`으로 보정했다.

세션 발급 PR 자체에는 ERD 변경이 없다. 후속 `feat/demo-contract`는 아래 데모 상태·초기화·날짜 이동 계약을 구현하며, 선택적 `/api/demo/ai`는 포함하지 않는다.

## 데모 구현 및 FE 재시도 안내

- `seed`는 `users.demo_seed`의 실제 선택값이다. 이전 DB의 선택 이력은 역추정하지 않고 null로 유지한다. 첫 seed/reset 이후에는 날짜가 바뀌어도 선택값을 유지한다.
- seed/reset/next-day는 같은 사용자 잠금을 사용한다. PostgreSQL에서는 다른 사용자가 독립적으로 진행하며, SQLite에서는 DB 단위 쓰기 잠금 때문에 다른 사용자도 일시적으로 `409 REQUEST_IN_PROGRESS`를 받을 수 있다. 클라이언트는 즉시 반복 호출하지 않는다.
- seed 요청은 `{seed:string}`이며 미지원 이름은 `400 BAD_SEED`, 타입 오류/알 수 없는 키는 `422 INVALID_INPUT`이다. reset/next-day는 빈 본문 또는 빈 객체를 받는다.
- next-day의 빈 계획은 `400 NO_PLAN`, 데모 날짜 없음은 `400 BAD_DATE`, 활동 중인 과목의 시험일이 없거나 현재 날짜가 마지막 시험일 이상이면 `400 DEMO_END`다. 실행/일시정지 중인 집중 세션은 `409 SESSION_RUNNING`으로 거절한다.
- 마지막 시험일은 이동 가능한 **도착 날짜**다. 마지막 시험일을 넘어 이동하지 않으므로 마지막 과목의 시험 다음 날 보관을 데모에서 강제로 재현하지 않는다. 중간 시험은 다음 날 보관한다. 시험 당일부터 미완료 배치를 제외하고 새 이월을 생성하지 않으며 완료 기록은 보존한다.
- `last_assessed_date`는 실제 마감한 날짜이고 `demo_date`만 다음 날이다.

`POST /api/demo/next-day`는 선택적 `Idempotency-Key: UUID`를 지원한다. FE가 재시도를 사용할 때에는 한 번의 [다음 날로] 동작에 키 하나를 만들고 **네트워크 재시도에는 같은 키**, 새로운 날짜 이동에는 새 키를 보낸다. 현재 FE `src/api/demo.js`는 키를 보내지 않으므로 기존 동작은 요청마다 별도의 하루 이동이다. FE 자동 재시도는 키 전달을 구현한 뒤에만 켠다.

- 키 누락: 매번 별도 요청. 빈 값/잘못된 UUID/중복 헤더: `422 INVALID_INPUT`.
- 키와 정규화한 요청 해시가 일치하는 성공 기록이 있으면 원래의 `200 data`를 반환하고 날짜·판정을 다시 적용하지 않는다. 표기만 다른 동일 UUID는 같은 키다.
- 처리 중 잠금 충돌은 `409 REQUEST_IN_PROGRESS`, 저장된 요청 해시 불일치는 `409 IDEMPOTENCY_CONFLICT`. 정의되지 않은 본문은 해시 비교 전에 422로 거절한다. 현재 유효 본문은 빈 명령뿐이다.
- 성공 응답과 모든 상태 변경은 같은 트랜잭션으로 저장한다. 실패 응답은 저장하지 않으므로 같은 키로 재시도할 수 있다. 이 기능은 전체 API의 공통 재시도 구현을 뜻하지 않는다.
- 재시도 기록은 이번 버전에서 자동 만료하지 않으며 seed/reset에서도 보존한다. 초기화 후 예전 키를 보내면 예전 응답을 반환할 뿐 초기화된 날짜를 바꾸지 않는다. 현재 상태는 GET state로 다시 조회하고 새 동작에는 새 키를 사용한다.

## 구현 규칙 (아래 원문 필드 표보다 우선)

- 새 세션은 DEMO_MODE일 때 day9, 일반 모드일 때 빈 사용자로 생성한다. 데모 비활성 상태에서 seed/reset/next-day는 403 DEMO_DISABLED.
- firstExam은 미래/당일 시험 중 가장 이른 것(동점 subjectId). 없으면 null. 지난 시험을 임의 대체하지 않는다. hasPlan은 항상 bool, seeds는 항상 object.
- seed는 실제 선택값을 저장해 반환한다. 날짜로 day9/overdue를 추정하지 않는다. seed/reset은 현재 사용자 데이터만 원자적으로 초기화한다.
- next-day는 사용자를 잠그고 하루를 마감한 뒤 날짜를 정확히 하루 증가한다. judgment는 carry/replan/none 중 하나, 일반 state/reset/seed는 null.
- 시험 종료 뒤에는 과목을 보관하며 다음 날 이동의 마지막 허용일은 마지막 시험일이다. 그 이후 요청은 400 DEMO_END.
- AI 시뮬레이션은 지원하는 개발 서버에서만 사용한다. slow는 35초, fail은 503 AI_FAILED; 사용자별 상태이며 seed/reset 시 null로 초기화한다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `POST /api/session` — 익명 ID 발급

| 응답          | 타입   | null 가능 | 설명                 |
| ------------- | ------ | --------- | -------------------- |
| `anonymousId` | string | 아니요    | 서버가 발급한 UUID |

#### `GET /api/demo/state` · `POST /api/demo/next-day` · `POST /api/demo/reset` · `POST /api/demo/seed` — 데모 상태

| 요청 (`/seed`만) | 타입   | 필수 | 허용 값                      |
| ---------------- | ------ | ---- | ---------------------------- |
| `seed`           | string | 예   | `empty` · `day9` · `overdue` |

| 응답 (네 경로 같음)   | 타입   | null 가능 | 설명                                                |
| --------------------- | ------ | --------- | --------------------------------------------------- |
| `today`               | string | 아니요    | 데모 날짜 `YYYY-MM-DD`                              |
| `firstExam`           | object | 예        | 오늘 이후 시험이 없으면 null |
| `firstExam.subjectId` | string | 아니요    | 가장 가까운 시험 (헤더 D-day)                       |
| `firstExam.name`      | string | 아니요    | 과목 이름                                           |
| `firstExam.examDate`  | string | 아니요    | 시험일                                              |
| `seed`                | string | 예        | 지금 시드 이름                                      |
| `seeds`               | object | 아니요    | `{ 시드이름: 메뉴 표시명 }` (데모 메뉴)             |
| `hasPlan`             | bool   | 아니요    | 계획이 있는지                                       |
| `judgment`            | string | 예        | next-day 직후 자정 판정 `carry` · `replan` · `none` |

#### `GET /api/demo/ai` · `POST /api/demo/ai` — AI 상태 재현 (/dev 전용)

| 요청     | 타입   | 필수 | 허용 값                                          |
| -------- | ------ | ---- | ------------------------------------------------ |
| `target` | string | 예   | `plan` · `quiz` · `grading` · `tutor` · `replan` |
| `mode`   | string | 예   | `null` · `slow` · `fail`                         |

응답: `{ plan, quiz, grading, tutor, replan }` 각 `null | "slow" | "fail"`. 개발용 선택 기능. 미지원 시 404.
