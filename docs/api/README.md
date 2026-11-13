# FE–BE API 명세 1.0

상태: **FE 전달용 계약 정리 완료 / 구현·통합 검증 전**. 구현 완료 문서가 아니다. 2026-11-13 작성.

FE의 11/12 22시 결정(`/api`, FE 응답 모양, 30초 요청, PDF만 작업 폴링)을 유지한다. 사용자의 충돌 해결 기준에 따라 아래 계약을 구현 기준으로 정리했다. FE 담당자가 이미 검토/승인했다는 뜻은 아니다.

## 문서 읽는 순서

0. [FE 전달용 변경 안내](frontend-handoff.md): 충돌 C01–C22, 수정 위치, 추가 API, 검증 범위
1. [원문 대조](source-audit.md): 요구사항 PDF·와이어 근거 및 별도 연동 확인 사항
1. [공통 계약](common.md): 인증, HTTP, 타입, 오류, 재시도
2. [결정 및 FE 변경](decisions.md): 원문 충돌 해소와 호환성 변경
3. [전체 경로](operations.md): HTTP method + path 단위 누락 확인
4. 도메인별 필드/업무 규칙: 경로 표의 링크
5. [ERD 매핑](persistence.md), [완료 판정 시나리오](acceptance.md)

FE가 기본이며 요구사항·와이어 충돌 시 기존 BE 명세, BE도 충돌하면 새 계약으로 교정한다. 상세 변경은 도메인별 문서에 반영하고 근거는 전달표에 남긴다. 설명용 예시의 과목/페이지/점수를 하드코딩하지 않는다. 문서 내 '위 S...'의 원본 예시는 [FE 고정 버전 초안](https://github.com/line1thon-team10/team-10_FE/blob/c8e250d9484a03eac005e12c24d1b703fc105626/docs/api-draft.md)에 있다. 구버전 OpenAPI는 대조 자료이고 현재 계약 생성용이 아니다.

## 출처와 범위

- 사용자 제공 요구사항 PDF와 와이어: [자료 식별·페이지별 근거](source-audit.md).
- FE `c8e250d9484a03eac005e12c24d1b703fc105626`: `docs/api-draft.md`, `docs/team-requests-1112.md`, `docs/policy-and-backend.md`, `src/api`, `src/mocks/handlers`, `src/config/app.js`.
- BE `3475cf204ac726f9c96d44bbc687ea8253f8c48e`: 현재 구현 비교 기준.
- `schema/erd.dbml`: 영속 구조 기준. `schema/openapi-0.7.0.json`의 `/api/v1` 경로와 쿠키/작업 API는 이번 계약에 적용하지 않는다.
- AI `services/learning_metrics.py`: 현재 지표 계산식을 검토한 참고 구현. 외부 AI의 필드와 FE 필드 변환은 BE 책임이다.
- 실제 시연용 시험일·목표·PDF별 단원 값은 계약이 아니라 별도 시드 데이터다. 수령하지 않은 값을 확정값으로 꾸미지 않는다.

## 현재 구현과의 관계

main에는 세션·데모·홈/기록·집중·퀴즈·책·밀림 일부가 구현되어 있다. 아래 모든 계약을 만족하지는 않는다. PR #2의 기본 저장소 추가도 기능 구현 완료를 뜻하지 않는다. 이번 브랜치는 문서만 변경한다.

제품 범위 밖: 로그인/계정 복구, 공통 202 Job API, AI 회고 화면. 신규 과목 추가/계획 재생성은 요구사항에 따라 지원하며 draft·미리보기·승인 계약을 정의했다. `/api/demo/ai`는 선택적 개발 기능이며 미지원 시 404, FE는 mock 모드에서만 노출한다.
