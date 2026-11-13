# 학습 기록 보정 계획 — 1단계

부모는 `feat/focus-contract` / PR #11 (`ebc7c3d`). 운영 배포는 별도 Sol 에이전트가 이 부모 SHA로 진행한다. 이번 브랜치의 새 코드는 이번 배포에 포함하지 않는다.

## 이번 범위와 판단 기준

Astra-max가 확인한 DB2 완료→p.13 정정 후 회독 근거가 그대로 유효한 문제를 먼저 해결한다. `docs/api/study.md`와 ERD learning_passes의 고정 근거 계약을 따른다. FE 입력 오류가 아니라 BE의 재판정 누락이다.

- 기록 쓰기는 demo/focus와 동일 사용자 잠금 아래 최신 사용자 상태를 읽고 실행한다. 같은 기록에 같은 종료 세션을 다시 보내는 것은 허용한다.
- 변경한 기록을 참조하는 기존 회독의 **고정 recordIds와 requiredRanges**를 다시 검사한다. 그 집합 안의 다른 기록들이 충분한 범위를 제공하면 유효 상태를 유지한다. 관계없는 새 기록을 임의로 끌어와 다른 회차의 근거를 재사용하지 않는다.
- 부족해지면 invalidated_at만 설정하고, 같은 근거가 복구되면 지운다. ID/순번/키/스냅샷/완료 날짜·시각, 기존 퀴즈·숙달도·과거 마감 분모를 보존한다. 반복 정정으로 새 회독을 만들지 않는다.
- 범위가 바뀐 단원도 당시 requiredRanges로 판단한다. 현재 단원 범위를 요구 범위로 덮어쓰지 않는다. 현재 생산되는 schemaVersion=1의 페이지 쌍 스냅샷을 지원한다. 명목 분량 스냅샷은 아직 생성 경로가 없으므로 이 단계에서 형식을 추측하지 않는다.
- 책 API round는 유효 행 수이며 0도 반환한다. 기존 FE 초안의 기본값1 및 BE max(pass_number)는 ERD의 유효 근거 계약과 충돌하므로 명세를 명확히 고친다. FE adapter의 `?? 1`은 숫자0을 보존한다.
- 블록의 현재 표시 상태는 정정한 태스크 상태로 재계산한다. stacked_at은 ERD상 첫 완료 시각이므로 지우지 않는다. 이월 계보의 책 중복 제거는 후속 단계다.

## 원자 단위

1. 공통 사용자 잠금 + 경합·최신 사용자 상태 테스트.
2. 동일 종료 세션 재전송 허용 + 같은 기록/다른 기록 재사용 테스트. 잠금과 독립적으로 설명 가능한 변경이므로 별도 커밋한다.
3. 기록 정정 트랜잭션에서 기존 회독 근거 무효화/복구 + 다중 기록·범위 구멍·다른 사용자/단원·실패 롤백 테스트.
4. 책 API 유효 회독 개수 + 0회/순번 구멍/다른 사용자 격리 테스트와 충돌 명세 수정.

SQLite 및 격리 PostgreSQL 17에서 검증한다. 각 단위는 검증 후 즉시 커밋하고, stacked PR에 기존 FE/BE/명세 충돌과 한계를 명시한다. 이슈는 만들지 않는다.

## 후속 범위

completedRanges 입력과 조회 ranges, 엄격한 UUID/본문 모델, 기록 멱등 응답 저장, initial/remedial 신규 회독 자동 생성, null-plan 직접 재학습, 블록 계보·마감 집계, P1 23:30, 퀴즈 savedGrading은 아직 완료한 것으로 표시하지 않는다.

## 1단계 검증 결과 (2026-11-14)

- 네 구현 단위를 각각 커밋했다. FE 전달 내용은 [study-record-handoff.md](study-record-handoff.md).
- Python3.14 / SQLite 및 격리 PostgreSQL17에서 `python -m pytest -q tests`: **425 passed, 1 skipped** (108.99초). 건너뜀은 SQLite에서 PostgreSQL 사용자별 잠금 독립성 검사이며 PostgreSQL 실행은 통과했다. 신규 검사는 두 DB 합계50개다.
- 범위 없는 `pytest`는 private AI submodule의 GROQ 키 필수 테스트 수집에서 실패한다. CI와 동일하게 BE `tests`를 명시해 전체 검증했다. 외부 유료 AI 호출 검증으로 표시하지 않는다.
- `scripts/check_api_spec.py --fe ../FE`: FE71 + 소스 유래5 경로와 Markdown 링크 검사 통과. 이 검사는 런타임 전체 계약 준수를 입증하지 않는다.
- 기존 PyMuPDF/SWIG deprecation 경고5개. 이번 코드 변경으로 새 DB migration은 없다.
