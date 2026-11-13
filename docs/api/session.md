# session API

## 세션 발급 구현 범위

`feat/session-domain`에서는 `POST /api/session`과 공통 익명 인증을 구현·검증한다. 요청 본문은 생략하거나 빈 객체를 보내며, 정의되지 않은 필드는 `422 INVALID_INPUT`이다. 응답에는 `anonymousId`만 포함하고, 사용자 내부 상태나 다른 사용자 ID를 추가하지 않는다.

- `app/domain/user.py`: UUID v4, Asia/Seoul, draft 및 빈 사용자 상태 생성.
- `app/repositories/users.py`: 사용자 저장·조회. 자체 commit 없이 요청의 트랜잭션에 참여한다.
- `app/services/session.py`: 사용자 생성과 선택적인 day9 시드를 같은 트랜잭션으로 적용한다. 시드 실패 시 생성된 사용자·관련 행을 함께 rollback한다.
- `app/core/auth.py`: UUID를 조회 전에 검증하고 사용자 레포지토리로 인증한다. [공통 계약](common.md)의 중복 헤더 규칙을 따른다.
- `/api/` 응답은 `Cache-Control: no-store`를 적용하며, 처리되지 않은 500 오류도 내부 정보 없이 no-store로 반환한다.

기존 FE mock의 `mock-...` ID는 mock 내부 값이며 실제 BE 자격이 아니다. FE의 실제 연동은 서버가 반환한 UUID를 보관하는 기존 흐름을 유지한다. 기존 BE가 잘못된 UUID를 DB에 직접 조회하던 동작은 `401 UNKNOWN_SESSION`으로 보정했다.

이 범위에서 ERD 변경은 없다. 아래 데모 API의 seed 지속 저장, firstExam 보정, next-day 중복 처리·종료일 제한은 **후속 데모 계약 작업**이다. 세션 생성 시 데모 시드 적재 검증이 데모 API 전체 구현 완료를 뜻하지 않는다.

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
| `anonymousId` | string | 아니요    | 예 `"mock-1a2b3c4d"` |

#### `GET /api/demo/state` · `POST /api/demo/next-day` · `POST /api/demo/reset` · `POST /api/demo/seed` — 데모 상태

| 요청 (`/seed`만) | 타입   | 필수 | 허용 값                      |
| ---------------- | ------ | ---- | ---------------------------- |
| `seed`           | string | 예   | `empty` · `day9` · `overdue` |

| 응답 (네 경로 같음)   | 타입   | null 가능 | 설명                                                |
| --------------------- | ------ | --------- | --------------------------------------------------- |
| `today`               | string | 아니요    | 데모 날짜 `YYYY-MM-DD`                              |
| `firstExam.subjectId` | string | 아니요    | 가장 가까운 시험 (헤더 D-day)                       |
| `firstExam.name`      | string | 아니요    | 과목 이름                                           |
| `firstExam.examDate`  | string | 아니요    | 시험일                                              |
| `seed`                | string | 예        | 지금 시드 이름                                      |
| `seeds`               | object | 예        | `{ 시드이름: 메뉴 표시명 }` (데모 메뉴)             |
| `hasPlan`             | bool   | 예 (true) | 계획이 있는지                                       |
| `judgment`            | string | 예        | next-day 직후 자정 판정 `carry` · `replan` · `none` |

#### `GET /api/demo/ai` · `POST /api/demo/ai` — AI 상태 재현 (/dev 전용)

| 요청     | 타입   | 필수 | 허용 값                                          |
| -------- | ------ | ---- | ------------------------------------------------ |
| `target` | string | 예   | `plan` · `quiz` · `grading` · `tutor` · `replan` |
| `mode`   | string | 예   | `null` · `slow` · `fail`                         |

응답: `{ plan, quiz, grading, tutor, replan }` 각 `null | "slow" | "fail"`. 개발용 선택 기능. 미지원 시 404.
