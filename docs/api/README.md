# FE–BE API 명세 1.0

상태: **원문 대조 반영 초안 / 미해결 계약 존재**. 구현 완료 문서가 아니다. 2026-11-13 작성.

FE의 11/12 22시 결정(`/api`, FE 응답 모양, 30초 요청, PDF만 작업 폴링)을 유지한다. 아래 명시한 보완은 이번 명세에서 제안하는 규칙이며 기존 팀 합의로 간주하지 않는다. 이 명세를 리뷰한 뒤 런타임 구현을 재개한다.

## 문서 읽는 순서

0. [원문 대조와 미해결 계약](source-audit.md): 요구사항 PDF·와이어 근거 및 기존 명세 오류
1. [공통 계약](common.md): 인증, HTTP, 타입, 오류, 재시도
2. [결정 및 FE 변경](decisions.md): 원문 충돌 해소와 호환성 변경
3. [전체 경로](operations.md): HTTP method + path 단위 누락 확인
4. 도메인별 필드/업무 규칙: 경로 표의 링크
5. [ERD 매핑](persistence.md), [완료 판정 시나리오](acceptance.md)

규칙 충돌 시 제공된 요구사항·와이어 원문을 먼저 대조한다. [원문 대조](source-audit.md)의 교정이 기존 decisions/common/필드 표보다 우선하며, 미해결 항목은 확정 계약으로 사용하지 않는다. 예시는 타입·값의 규범이 아니며 과목/페이지/점수를 하드코딩하지 않는다. 문서 내 '위 S...' 참조의 원본 예시는 [FE 고정 버전 초안](https://github.com/line1thon-team10/team-10_FE/blob/c8e250d9484a03eac005e12c24d1b703fc105626/docs/api-draft.md)에 있다. 이번 명세의 보완 규칙이 원문보다 우선한다.

## 출처와 범위

- 사용자 제공 요구사항 PDF와 와이어: [자료 식별·페이지별 근거](source-audit.md).
- FE `c8e250d9484a03eac005e12c24d1b703fc105626`: `docs/api-draft.md`, `docs/team-requests-1112.md`, `docs/policy-and-backend.md`, `src/api`, `src/mocks/handlers`, `src/config/app.js`.
- BE `3475cf204ac726f9c96d44bbc687ea8253f8c48e`: 현재 구현 비교 기준.
- `schema/erd.dbml`: 영속 구조 기준. `schema/openapi-0.7.0.json`의 `/api/v1` 경로와 쿠키/작업 API는 이번 계약에 적용하지 않는다.
- AI `services/learning_metrics.py`: 현재 지표 계산식을 검토한 참고 구현. 외부 AI의 필드와 FE 필드 변환은 BE 책임이다.
- 실제 시연용 시험일·목표·PDF별 단원 값은 계약이 아니라 별도 시드 데이터다. 수령하지 않은 값을 확정값으로 꾸미지 않는다.

## 현재 구현과의 관계

main에는 세션·데모·홈/기록·집중·퀴즈·책·밀림 일부가 구현되어 있다. 아래 모든 계약을 만족하지는 않는다. PR #2의 기본 저장소 추가도 기능 구현 완료를 뜻하지 않는다. 이번 브랜치는 문서만 변경한다.

제품 범위 밖: 로그인/계정 복구, 공통 202 Job API, AI 회고 화면. 신규 과목 추가/계획 재생성은 요구사항에 있으며 FE 축소 결정과의 릴리스 범위 충돌로 확인이 필요하다. rebuild의501은 현재 FE 축소 동작일 뿐 최종 제품 계약이 아니다. `/api/demo/ai`는 선택적 개발 기능이며 미지원 시 404, FE는 mock 모드에서만 노출한다.
