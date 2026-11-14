# 병렬 구현의 내부 계약

2026-11-14, SYS-02. 외부 API는 `docs/api`를 따른다. 이 문서는 별도 작업트리에서 구현한 도메인들을 연결할 최소 경계이며, 구현 완료 선언이 아니다.

## 범위 계산 (R-01)

`app/domain/ranges.py`에 immutable `PageSpan(material_id: str, start: int, end: int)`과 `normalize`, `page_count`, `subtract`, `intersect`, `covers`를 제공한다. 함수는 PageSpan의 iterable을 받고 normalize/subtract/intersect는 정렬된 tuple[PageSpan,...], page_count는 int, covers(required, completed)는 bool을 반환한다. 페이지는 양끝 포함이다. material_id가 다르면 같은 숫자를 합치지 않는다. bool/float/문자열 페이지, 빈 material_id, 0/역전 범위는 ValueError다. 빈 범위는 허용하며 단일 span에 페이지 없는 뜻을 넣지 않는다. 큰 PDF도 쪽마다 set을 만들지 않고 구간으로 계산한다.

이 계산은 unitId/conceptId/rangeId를 대체하지 않는다. 저장·소유권·단원별 회독은 상위 서비스가 식별자를 유지하고 같은 단원의 근거만 넣어 판정한다. R-02는 요청 rangeId를 현재 소유 태스크의 실제 범위에 대응시킨 뒤 전체 교체 스냅샷을 저장한다. 다른 단원이나 다른 회차의 기록을 임의로 완료 근거에 섞지 않는다.

## 조회 경계 (R-03 / D-01)

TaskView의 ranges는 서버 순서의 `{id,materialId,unitId,conceptId,from,to,doneTo}`다. from/to/unitNo/doneTo의 최상위 단일 범위 표시는 실제 범위 하나일 때만 사용하며 복합은 null이다. pages/donePages는 materialId별 합집합이다. 완료 기록의 unit별 범위를 task 범위와 교차해 doneTo를 산출하며 없는 완료는 null이다. 페이지 없는 태스크는 ranges=[] 또는 명세에 맞는 명목 범위로 처리하되 pages=0과 result 완료 여부를 섞지 않는다.

진행률 계산은 task별 계획·완료량, 과목의 고유 새 범위, 출제 가능한 범위의 세 종류다. 출력 rate는 HALF_UP과100상한, 실행 가능 여부는 반올림 전 정수 분자·분모 비교를 사용한다. D-01이 이를 담당하며 다른 서비스가 표시된 rate를 다시 판정 근거로 쓰지 않는다.

## 퀴즈 식별·확정 경계 (Q-01 / Q-02~05)

공개 questionId는 quiz_items.id이며 원본 questions.id가 아니다. 서비스 내부에서는 item_id와 question_id를 구분하고 점수·취약 문항에는 원본 식별자를, 답안 수정·제출에는 세트 항목 식별자를 사용한다. GET에는 quizId를 포함하고 submit은 그 quizId로 대상을 고정한다. 과목 경로와 세트의 소유 과목 불일치는 NO_QUIZ다. 풀이 중 정답·해설·출처 페이지·채점 상태는 노출하지 않는다.

Q-02는 저장된 답안 revision/hash와 채점 작업 lease, 세트 봉인 및 결과 스냅샷을 설계한다. Q-03은 답안 저장과 작업 접수를 원자 commit한다. Q-04는 트랜잭션 밖에서 호출 후 현재 revision/lease가 일치할 때만 결과를 저장한다. Q-05만 확정 풀이를 집계 입력으로 승격한다. 확정 사실은 userId/quizId/itemId/questionId/unitId, answerRevision, 확신도·정오·점수, answeredAt/answeredDate를 보존하며 단순 같은 단원 code로 다른 자료를 합치지 않는다. 새 필드 없는 기존 경로의 Q-01 보정은 Q-02 이후의 답안 내구성을 완료했다고 표시하지 않는다.

## 사용자·날짜·트랜잭션

쓰기 요청은 기존 `lock_user`와 같은 사용자 행 잠금을 사용하고 잠금 뒤 사용자 상태를 다시 읽는다. 순서는 user → plan/quiz 등 해당 aggregate다. AI 외부 호출 동안 사용자 잠금을 유지하지 않는다. 최종 반영 시 다시 잠금과 버전 검증을 수행한다. 잠금 경합은409 REQUEST_IN_PROGRESS이며 500으로 바꾸지 않는다. 오늘은 demo_date 우선, 일반 사용자는 사용자 timezone의 서버 날짜다. 한 요청의 판단 시각은 한 번 캡처한다.

초기 P1 구현은 기록 요청 시점의 `(모두 기록 OR23:30 이후) AND실제70%` 평가와 하루 한 번 저장을 담당한다. 별도 실시간 푸시/새 API를 임의로 추가하지 않는다. 일반 날짜 누락 마감은 D-04가 처리하며 다른 GET이 임의로 demo 날짜를 증가시키지 않는다.

## Preview 경계 (SYS-04/05)

후보는 owner, kind, normalized input/hash, 기준 날짜, plan revision(계획 없으면 자료·목표·일정 ID/updated_at 해시), 관련 자료/일정/기록 버전, 실제 변경안, 상태와 최초 응답을 가진다. 생성만으로 승인 상태를 바꾸지 않는다. apply는 저장된 변경안만 사용하고 동일 후보 재시도는 최초 응답을 반환한다. 기준 불일치는409 PREVIEW_STALE, 임의30분 유효기간은 없다. 도메인은 현재 의존 버전 계산과 후보의 실제 실행 함수를 제공하며 공통 서비스가 도메인 SQL을 추측하지 않는다.

SYS-05는 caller 트랜잭션 안에서 사용자 잠금과 최신 사용자 재조회를 수행한다. 한 요청의 UTC 판단 시각을 한 번 잡고, 그 시각과 최신 사용자 timezone/demo_date로 기준 날짜를 계산한다. 도메인 콜백에는 잠금 후의 사용자와 저장된 변경안만 전달하며 외부 AI 호출을 하지 않는다. 같은 applied 후보의 응답 재생은 owner/kind/target 확인 후, 현재 날짜·의존 버전 검사보다 먼저 처리한다. legacy 최초 응답 누락은 decisions.md의409 정책을 따른다.

승인 계획이 있는 변경은 plans.revision을 현재 기준값에서 정확히 한 번 증가시킨다. 최초 승인으로 계획을 만드는 경우 초기 revision은1이고, 승인 전 draft만 교체하는 작업은 승인 계획을 만들거나 revision을 증가시키지 않는다. 이 세 실행 유형은 서버 도메인이 명시적으로 선택하며 요청 body가 선택하지 않는다. 도메인 SQL·revision 변경·후보 applied/최초 응답 저장 중 하나라도 실패하면 같은 트랜잭션의 변경 전체가 되돌아가야 한다. 내부 savepoint를 쓰더라도 사용자 잠금보다 먼저 열지 않는다.

## 근거 형식·정책의 미정 범위

기존 page-backed learning_passes의 schemaVersion="1", recordIds, requiredRanges=[[start,end],...]는 보존한다. 정정은 저장된 recordIds만 검사하고 동일 행의 자격만 바꾼다. 새 회독 생성 B-01/02와 직접 재학습 V-04가 새로운 근거 소비를 소유한다.

페이지 없는 명목 분량 근거의 저장 형식과 직접 재학습 보완 입력은 현재 Markdown/ERD 차이가 있으므로 B-01/V-04 착수 시 해당 계약·migration task로 먼저 확정한다. 다른 기능에 임의 형식을 퍼뜨리지 않는다. 0.7 OpenAPI의 별도 endpoint를 현재 /api 계약에 그대로 추가하지 않는다. 이러한 범위가 확정되지 않았다는 이유로 R-01/Q-01/O-01을 중단하지 않는다.

계획 수치 함수는 POL-01, 운영 한도/시간 예산은 POL-02, OCR 품질 신호는 POL-03에서 각각 결정한다. 기존 정책을 재확인해 쓸 수 있는 값과 새 제품 판단이 필요한 값을 구분한다. 미정 값을 성공처럼 반환하는 stub을 만들지 않는다.
