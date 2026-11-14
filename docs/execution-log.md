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

## R-02 최종 통합과 다음 검토

- PR #19 최종3426dbc는 이월/제외된 task의 기록을 정정해도 배치 상태와 자식 이력을 되살리지 않는다. 기존 계약의 PATCH 제한도 수정해, 이미 있는0쪽 기록을 전체 범위로 정정할 수 있도록 명시했다.
- 독립 검토의 원래 실패6개를 immutable archive에서 재실행하여 모두 통과했다. Actions [43541475416](https://github.com/line1thon-team10/team-10_BE/actions/runs/43541475416)의 후보6b09c45는1101 passed/1 skipped. 로컬 통합f89d50a의 파일 tree6fe98d0는 해당 후보와 정확히 같다.
- P-01은 하루 단위 동점 처리 때문에 후순위 예약 목표를 놓치는 반례를 발견했다. 정책을 줄이지 않고 전체 예약 우선순위를 먼저 비교하도록 수정 중이며 원래 PR CI 통과를 수정본 승인으로 간주하지 않는다.
- QA-01은 실제 등록된 경로의 인증/소유권/오류 경계를 검사하는 독립 harness를 개발 중이다. 온보딩의 잘못된 draft 컨텍스트는 기존 명세대로404이며 모든 잘못된 ID가422라는 가정은 적용하지 않는다.

## 배분·공통 계약 검사의 통합

- P-01 e3df828: 독립 원래 반례 포함229tests, 전역 목적함수1000개(273가능/727불가) 및 추가feasibility500개 모두 일치. 90/180/360쪽 비교에서 큰 회귀는 관측하지 않았으나180쪽9단원은239620노드로250000예산에 가까웠다. SearchLimit을불가능으로오인하지 않는다. Actions43543104127의1174passed/1skipped를 확인하고b395057로통합했다.
- QA-01 970a99b: 독립11tests와 중첩/숨김/늦은라우트등록/실제CLI검사 통과. required74 중23등록/51대기를남기며최종게이트는실패한다. Actions43543144344는1112passed/1skipped. c8f5e00로통합후P01+QA01 영향238개통과.
- Q-02는독립검토192+10개통과/잔여findings없음,실제CI대기. AI-01은root가JSONB U+0000저장실패를재현해보완했고161회귀통과후별도검토중이다.
- 다음병렬구현은R-03홈/집중범위응답과S-01숙달도이다. notice/pace의범위필드는각각L-01/L-04에서실제근거를연결한다. 새운영배포는아직없다.

## 답안 저장 통합과 공통 경로 재검증

- Q-02 PR #22를2cdc874로 통합했다. 정확히 이 SHA에서 전체 SQLite/PG 회귀는 **1377 passed, 1 skipped**(365.56초), Actions43546105252도 성공했다. Alembic head는0006_quiz_answer_storage다.
- AI-01은JSON NUL 외에도 작업 삭제 경쟁의 usage 유실, 큰 spawn 입력의 시간 초과, 일부 transport 실패 은폐를 독립 검토에서 발견했다. 최종1719ec2의169개 영향 테스트와 원래9개 재현/151개 gateway 재검증이 통과했다. 최종 PR CI는 별도 확인한다. 실제 외부 AI 호출 검증은 아니다.
- R-03 7beb500은424개 영향 테스트, 독립 HTTP4개, 범위 오라클5000건 통과 후 PR #24 CI 대기다. S-01 53db4e4는 계산 오라클은 통과했으나 기존 데모 회귀 테스트가 변경 전 함수를 호출해 실패했다. 별도 테스트 연결 보정 후 다시 확인한다.
- 다음 구현은 SYS-04(0007 schema 독점)와 O-02(app/main.py 최소 등록 독점)다. O-02 조사에서 기존 KST seed가SQLite에서9시간 밀릴 수 있음을 발견해 UTC 정규화를 별도 원자 변경으로 배정했다.
- 운영 배포는 아직 변경하지 않았다. 기존 데이터와 원본 FE/BE/AI 작업트리를 보존하고 있다.

## 범위 응답·AI 실행·숙달도 통합

- R-03 PR #24를e11d961, AI-01 PR #23을bea6d7d, S-01 PR #25를4c20454로 통합했다. 각 최종 PR CI 성공을 확인했다. 정확히4c20454 전체 SQLite+PG는 **1677 passed, 1 skipped**(439.70초)다.
- SYS-04는 PostgreSQL JSONB 숫자의 해시 변화와 조회 중 상태가 섞이는 검토 지적을 각각8a65750/3e7abe6으로 보정했다. 최종13f1a82의 독립 재현30개 통과 후 Actions43548622122에서1562 passed/4 skipped. 통합7dd2d4의 영향 검사는406 passed/3 SQLite 전용 경쟁 검사 skip이며 Alembic 단일 head0007이다.
- O-02는 기존 초 단위 일정의 메타데이터 PATCH가 실패하거나 초를 버리는 P2를7b13e11에서 수정했다. 독립 원래10개+추가10개 및215개 회귀 통과, 최종 Actions43549792768을 기다린다.
- D-01 b9c76a5는 작업자745개, 독립751개 SQLite+PG 검사와 페이지 집합 오라클3000건이 통과했다. 원 비율69.99%는 잠기고70%에서 열리며 정정하면 다시 잠기는 HTTP 경계를 확인했다. 최종 Actions43550278524 확인 후 통합한다.

## 다음 구현과 고정 배포

- SYS-05를f22db92 기준 별도 작업트리에 배정했다. 이미 적용된 후보의 최초 응답 재생, legacy 원 응답 누락 시409, 기존/최초/승인 전 계획의 revision 경계를 명세에 보완했다. PR에서 기존 BE 계약의 누락임을 설명한다.
- Q-04는 서버 재시작 후 saved/backup 작업이 live로 바뀌지 않도록0008 실행 모드를 영속화한다. schema와 generator를 독점하며 공통 생성기 지원·schema·기존 테스트 보정·운영 worker 연결을 별도 커밋한다. 실제 AI 키 없는 가짜 transport 검증과 실제 provider 품질 검증은 구분한다.
- FE-01은 원격 main/dev가c8e250d인 것을 다시 확인하고 문서 전용 작업트리에서 시작했다. 기존 사용자 기준과 C01~C22 변경 위치, 실제 로컬 응답 fixture를 준비한다. FE 화면 연결은 후속 task다.
- 중간 배포는 앱4c20454/AI6d74ffb/DB0006, workflow76ed718을 고정했다. Sol 담당자의 Actions43550194880이 GitHub-hosted ARM64 테스트·빌드를 진행 중이며, 이 기록 시점에 RPI 적용은 시작하지 않았다. 새 통합HEAD를 해당 실행에 섞지 않는다.

## 일정·진도 통합 검증과 운영 체크포인트 완료

- O-02 최종 CI43549792768 후보ee905a3에서1872 passed/1 skipped, D-01 CI43550278524 후보2518106에서1743 passed/1 skipped. 각각6319d3b/9d1ddd7로 통합했다. **정확히9d1ddd7 전체 SQLite+PG는2123 passed/4 skipped,595.38초**다. 로그 `/private/tmp/team10-integration-progress-events-full.log`.
- Sol 배포 run43550194880은 build/deploy 모두 성공했다. 앱4c20454·AI6d74ffb·DB0006, digest `sha256:f5250def3a82f5934cb832a6ad08c6b9a3710075415208c86de69275f2a52f53`. root도 RPI4 API 컨테이너의 실제 image reference/ID를 읽어 같은 digest를 확인했다. ARM64는1677 passed/1 skipped이며 운영 성공은 최신 통합9d1ddd7 배포를 뜻하지 않는다.
- 쓰기 중지 후 백업은 `/home/ubuntu/apps/2026-line1/backups/20261114T032713Z.sql`(3,030,248 bytes,600,SHA256은release manifest). 공개 day9 42분 집중 종료→p.13 기록→67%에서74%, 퀴즈 항목 UUID·준비도·no-store를 확인했다. 루트복구 문서 갱신과 보고 커밋55e108e를b3c3ff1로 병합했다. 외부 AI/OCR 실키 확인은 없다.
- Q-03 독립 답 저장/GET 복구는9d1ddd7에서 시작했다. 기존 legacy submit에는 잠금/채점 분리 경계가 없으므로 AI 대기 전체를 잠그는 보정은 거절했다. Q-03 단독 운영 배포를 금지하고, Q-05의 실제 HTTP save/submit 경쟁 검사 및 트랜잭션 밖 채점 완료를 배포 조건으로 기록했다.
- FE-01은 FE의전용docs브랜치에 기준·C01~C22매핑(fc706af)과14개HTTP응답/5fixture/재캡처도구(4c715bf)를 원자 커밋했다. 임시SQLite에서 캡처했으며 운영 데이터·키·FE runtime을 바꾸지 않았다. BE 및 루트 전달 문서의 ‘명세만 작성’이라는 오래된 상태도 갱신했다.

## 답안·worker 독립 보완 및 최신 FE 반영

- Q-03(PR #29)의 독립 검토가 문자열 저장, concept 소속, 문항 변경 후 영수증 재생의 세 P2를 검출했다. 각각7d1f49d/83acd27/4022fa7에서 보정했으며, 원본 재현22개와 추가 독립·변경 회귀70개가 SQLite·PG에서 모두 통과했다. 문서 포함0f746ed이며 Q-05 선행 배포 조건은 유지한다.
- Q-04는92cd382의706개 검사 외에 손상 퀴즈 선두100개가 정상 작업을 영구 지연하는 독립 재현2개가 실패했다. 전체 퀴즈 검증을 재사용하고 keyset·cursor·공유 deadline으로 보완했다. b6f3a3d에서 작성자361개 및 원래2개가 통과했으며 별도 검증자가 보완을 다시 검사 중이다.
- SYS-05(PR #30) 최종a56e5af는397 passed/5 SQLite 전용 skip(PG에서 해당 경쟁 검사 실행). 오래된 writer의 알려진 적용 사실과 후보 kind 오류도 최소 repository 보완으로 구분했다. Astra가 고정 SHA를 독립 검토 중이다.
- FE-01은984423d에서 독립 검증 완료(5fixture/14HTTP 재캡처, null 세션 입력422/생략200 안내 보정). 이후 담당자의 main/dev가49c95c9로 전진한 것을 발견해 정상 mergea19164a로 보존하고564224a에서 인계 문서만 갱신했다. 합친 코드의 npm check 통과, 문서 delta 재검토 중이다. 기존 hybrid·프록시·새 시드·firstExam 보정을 후속 FE 작업에서 재사용한다.
- 11/14 04:08 UTC 공개 GET demo/state에 잘못된 익명 ID를 보내401 UNKNOWN_SESSION 및 no-store를 확인했다. FE가 과거 보고한500은 현 배포에서 재현되지 않았다. 새 운영 배포는 수행하지 않았다.
