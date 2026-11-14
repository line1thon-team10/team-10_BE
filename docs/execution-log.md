# 개발 실행 기록

## 2026-11-14 시작

- 사용자 승인: 계획의 task를 서브에이전트로 계속 실행하고 큰 기능 묶음마다 별도 검증 에이전트를 둔다.
- 통합 작업트리: `BE-integration`, 브랜치 `integration/contract-v1`. 계획 문서 `8db364b`에서 시작하며 앱 기준은 PR #13 `f47eac6`이다.
- 원격 fetch 뒤 PR #13 브랜치와 앱 기준 SHA 일치 확인. 원본 BE/FE/AI 작업트리는 보존했다.
- AI submodule은 `6d74ffb6c455a306e912252013d76f93e30a6210`로 초기화했다. 공유 Python venv는 읽기용 링크로 사용하며 패키지를 동시에 변경하지 않는다.
- 초기 파일 배정: R-01 `app/domain/ranges.py`; Q-01 퀴즈 router/service/schema; O-01 온보딩의 신규 모듈. 필요 시 O-01에만 schema 및 `app/main.py` 등록 변경을 예약한다.
- 실제 신규 운영 배포는 아직 없다. 마지막 검증된 운영 앱은 `ebc7c3d`이며 릴리스 때 재확인한다.

## R-01 통합

- PR #15, 검토 HEAD `d4dc56336db4b5ad3276b9d11673ffa085d54127`. 순수 범위 함수만 변경.
- 구현/통합 테스트 각각154 passed. Astra-max 별도 비트마스크 오라클65,536쌍 및 무작위10,000쌍 일치, 매우 큰 경계 검증. 재현 가능한 결함 없음. DB 소유권/단원별 범위 선택은 후속 서비스에서 검증한다.
- 통합 commit `85a32738706e15945270c8818c50d2f90f2a54d3`. 운영 배포는 아직 변경하지 않았다.
- POL-01/02/03은 문서와 pinned AI/요구사항을 대조해 결정했으며 독립 검토 중이다. 실제 OCR/외부 AI 호출 검증과 구분한다.

## POL-01/02/03 검토 완료

- 상세 계산은 제품 원문과 구분한 BE 정책이다. Astra 검토에서 속도 난이도 중복 배수와 직전일 예약 경합이 발견되어 `363d652`, `a05ebf0`로 보완했다. 최종 SHA에 미해결 차단 지적 없음.
- 품질 기준은 pinned AI 휴리스틱과 실제 네이티브 PDF/합성 추출 결과로 대조했다. 실제 OCR은 아직 검증하지 않았다. 업로드/AI 예산의 실제 강제는 각 구현 task에서 검증한다.
- Q-01은230개 SQLite/PG 테스트를 통과한 immutable `b2565de`로 독립 검토 중. R-02는 `a05ebf0` 기준 별도 worktree에 배정했다.

## 첫 기능 묶음 통합 검증

- 통합 SHA `62592888c36bd6f99c97d9e1d31620df42a26755`: R-01/Q-01/O-01. 전체 `python -m pytest -q tests --tb=short`에서 **889 passed, 1 skipped**(179.76초). SQLite와 격리 PostgreSQL schema 사용. Alembic 단일 head `0005_onboarding_state`.
- Q-01 독립 검토는220 회귀+6개 추가 엣지 통과. O-01은104 회귀+5개 추가 검증 통과 후 Decimal 변환 경계 문제4개를 재현했다. `334cec8`에서 원본 Decimal을 먼저 검사하도록 수정하고 root80개/독립30개(원래 실패4개 포함) 통과.
- PR #15/#16/#17의 실제 변경을 통합했다. 후속 답안 저장/채점, 일정 쓰기/자연어 대화까지 완료한 것은 아니다. 새 운영 배포는 아직 없다.
- OPS-01(PR #18) 초판의 실제 GitHub ARM64 CI는579 passed/1 skipped. 이후 배포 라벨 guard와 과거 앱의 도구 분리를 추가하여 새 SHA를 다시 검토 중이며 초판 통과를 새 SHA 통과로 대신하지 않는다.

## OPS-01 최종 통합

- 최종 검토 `0bfbe9ecf0999b6a3ad2d61a82f6487a1f2837e1`. 과거 앱 SHA의 오래된 apply.sh가 라벨 검사를 건너뛰는 P2를 발견해, Compose는 앱 SHA/apply.sh는 workflow SHA로 분리했다. 독립 모의 실행29개 통과, 잔여 findings 없음.
- Actions [43539867574](https://github.com/line1thon-team10/team-10_BE/actions/runs/43539867574)의 실제 후보 merge `896209c`(0bfbe9e + 6259288), pinned AI `6d74ffb`: **900 passed, 1 skipped**,344.07초. 이미지 발행·RPI 배포 없는 PR 테스트다.
- 로컬 통합에서도 배포 실패/라벨/복구 경계21개 통과. Q-02는 schema lease0006으로 진행 중, R-02는 이월/excluded 배치가 기록 정정으로 부활하는 검토 지적을 수정 중이다.
