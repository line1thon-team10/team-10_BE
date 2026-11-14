# S-01 숙달도 규칙 인계

2026-11-14. 확정 풀이 기반 순수 계산과 기존 quiz service의 최소 연결이다. 원본 private AI, schema, seed, study 조회 서비스, 저장 채점 API, 보충/재학습 생성은 변경하지 않았다. API 계약의 수치 근거는 books/review이며, 원문 pp.9/10/18/19의 확인 결과에 따른다.

## 입력과 결과

`app/domain/mastery.py`의 `ConfirmedAttempt`는 attempt/item/question/material/unit/concept UUID, 확정 정오·확신도·난이도·세트 종류, answered_date/answered_at/finalized_at을 가진 immutable fact다. 개념 없는 문항은 concept_id=null을 유지한다. 날짜→UTC answered_at→attempt ID로 안정 정렬하고 같은 attempt ID의 동일 fact는 한 번만 계산한다. 같은 ID의 다른 내용, 동일 quiz item의 두 확정 fact, 문항의 충돌하는 단원/난이도 메타데이터는 거절한다. 다른 세트에서 같은 원본 문항을 다시 푼 실제 attempt는 별도 사실이다.

`weighted_score`는 확실 정답1, 애매 정답1/2, 오답0과 low=1, mid=3/2, high=2 가중치를 Fraction으로 계산한다. 전체 가중률 A와 최근 최대20건 B를 사용하며 alpha는 최근 건수9 이하1/2,10~19는2/5,20은3/10이다. 빈 입력은 점수 없음(None), 실제 오답만 있는 입력은0이다.

`calculate_mastery(attempts, recovery_entries=(), current_scopes=(), eligibility=())`는 날짜별 history, 단원별 최신 결과, 개념별 오답 수를 반환한다. 단원은 UUID로 분리하고, 개념 없는 오답은 해당 material/unit의 null 개념 버킷에 보존한다. None과 문자열을 직접 정렬하거나 가짜 개념을 만들지 않는다. 이 오답 집계는 새 review endpoint나 보충 태스크 생성까지 완료했다는 뜻이 아니다.

`DayScore`의 값은 다음과 같이 다르다.

| 값 | 의미 |
| --- | --- |
| raw_score | 가중식 결과0~100, 모든 상한 적용 전 Fraction |
| capped_score | 99·일일 회복·등급 제한 후 Fraction, 다음 날짜의 기준 |
| score | capped_score를 HALF_UP한0~99 정수 |
| grade | 표시 점수 기준 risk<50, shaky<80, stable>=80 |
| daily_increase_limit | 회복이면5 또는15, 회복 전에는 null |
| recovery_result_dates | 회복 진입일부터 실제 확정 결과가 있는 서로 다른 날짜 |

DB의 기존 `mastery_history.raw_score`에는 상한 전 값을 저장하고, `score`와 `units.mastery_score`에는 제한 후 정수를 저장한다. 기존 Numeric(asdecimal=False) schema 때문에 저장 raw는 float로 전달하지만 다음 점수 계산의 근거로 그 근삿값을 다시 사용하지 않는다. 확정 facts에서 Fraction 계산을 재생한다. 100점 원점수와99점 표시를 섞지 않는다.

## 날짜와 실제 회복 진입

하루의 확정 풀이를 모두 모아 전날 capped_score 대비 상승5점, 같은 날짜/현재 범위의 전체 재학습과 custom 확정 풀이 근거가 모두 있으면15점으로 제한한다. 하락은 제한하지 않는다. 여러 제출로 그날의 기준을 매번 갱신하지 않는다. risk에서 stable로 바로 상승하는 계산은79로 제한한다. 회복 중 stable은 검증된 보충 완료와 서로 다른2일 이상 결과가 필요하다.

`RecoveryEntry(material_id, unit_id, recorded_date, displayed_score, history_id, recorded_at)`는 실제 확정 세트의80미만 표시 결과다. 서버는 기존 mastery_history를 읽어 전달하며, (recorded_date, recorded_at, history ID) 순서의 최초 진입을 쓴다. 동일 history ID의 충돌 내용은 거절한다. 회복 진입은 이후 합산 점수가 올라가도 유지한다. 입력 배열 순서와 같은 fact의 반복은 결과를 바꾸지 않지만, 실제로 먼저 확정되어 표시된 세트 이력의 차이는 보존한다.

원문에서 상세히 정하지 않은 두 경계를 이번 BE 세부 결정으로 books에 추가했다.

- 회복2일에는 최초 표시 점수80미만의 결과 날짜를 포함한다. 진입 전 날짜는 제외하고 같은 날 세트가 여러 개여도1일이다. D0=79, 보충 완료, D1 결과면2일 조건이다.
- 전날 결과가 없는 첫 측정일에는 그날 최초 실제 회복 진입 표시 점수가 기준이다. D0 첫 세트70→추가 풀이 원점수90이면75가 최대다.15점 근거가 있어도2일 미달이면79다. 첫 세트부터90이면 회복이나0점 기준을 만들지 않는다. 문항 prefix로 가상의 낮은 세트 결과를 만들지 않는다.

현재 세트는 먼저 전체 finalization을 저장하고 계산한 결과를 같은 트랜잭션의 mastery_history로 남긴다. 그 결과가80미만이면 다음 재생에서 실제 RecoveryEntry가 된다. 과거 확정 facts의 날짜별 낮은 결과는 그대로 재생하지만, 기존 seed/unit의 recovery_mode나 recovery_started_at만 보고 존재하지 않는 확정 이력·보너스·보충 완료 근거를 생성하지 않는다. 실제 서버 시각과 데모 날짜가 다른 문제는 recorded_date로 해결한다.

## S-03/V-04가 제공할 근거

`UnitScope(material_id, unit_id, revision)`은 현재 자료/단원 범위를 고정한다. adapter의 revision은 자료 SHA/분석 버전, 단원 페이지·quantity basis·in_scope, 현재 개념 ID/페이지/in_scope를 해시한다. 숙달도 갱신 시각은 포함하지 않는다.

`DayEligibility(scope, day, full_relearned, custom_confirmed, supplement_completed)`는 서버가 이미 검증한 현재 범위의 근거 snapshot이다. 다른 material/unit/revision/날짜의 전체 재학습+custom 조합은15점 자격이 아니다. 동일 날짜에 실제 custom 확정 fact도 있어야 한다. 재학습만 또는 custom 풀이만으로 상한을 열지 않는다. 보충 완료는 회복 진입 이후의 현재 scope 근거여야 한다. 정정으로 무효가 된 근거는 S-03이 다시 검증해 이 입력에서 제거한다.

이번 DB 연결은 eligibility를 비워 둔다. 임의 learning_passes 행, legacy recovery 플래그, 단순 제출 횟수를 완료 근거로 승격하지 않는다. 따라서 현재 연결에서 회복 상승은 기본5점이고, 검증된 보충 근거 없이 stable로 올라갈 수 없어79점에 제한된다. 순수 계산의15점/안정 자격 입력과 실제 근거 생성·검증 완료를 구분해야 한다. S-02 보충 생성, S-03 근거 판정, V-04 전체 재학습 완료 연결이 후속이다.

## 현재 DB 연결의 범위

`app/ai/metrics_adapter.py`는 native DB UUID snapshot을 순수 계산으로 변환하며 private AI를 호출하지 않는다. `_metrics_input`은 현재 소유 단원과 해당 원본 문항을 읽고 세트 소유자, 과목 대응, submitted/finalized/completed 상태와 attempt finalized/graded/정오 확정을 확인한다. 오늘 이후 날짜는 집계하지 않는다. 같은 세트/단원의 기존 mastery_history가 있으면 `_recalc_mastery`는 다시 점수를 적용하거나 이력을 만들지 않는다.

현재 DB 모델은 질문 난이도/개념 메타데이터를 questions에서 읽는다. Q-05는 확정 사실의 메타데이터가 나중에 변경되지 않도록 보존해야 한다. 이 함수에는 전체 확정 이력을 전달해야 하며 최근20개만 조회해 전체 A를 대체하면 안 된다. SQLite가 timezone 정보를 제거한 timestamp는 기존 저장 규약에 따라 UTC로 정규화한다. answered_date는 별도로 보존한다.

기존 seed package는 원본 learning_metrics를 호출하는 별도 로컬 생성 경로이며 이번 task에서 변경하지 않았다. 현재 고정 seed의 최초 단원90/40/80은 같은 규칙으로 유지된다. 기존 quiz의 답안 저장·서술형 비동기 채점·사용자 잠금·멱등 submit 전체는 Q-03/Q-04/Q-05의 작업이다. 이 연결만으로 그 작업들을 완료했다고 표시하지 않는다.

## FE/BE 차이와 수정 이유

| 기존 동작/표현 | 수정 이유와 인계 |
| --- | --- |
| DB UUID를 unit/concept/question code로 바꿔 AI 입력 구성 | 다른 PDF의 같은 code가 합쳐져 잘못된 점수를 만드는 BE 버그. UUID로 계산하며 FE도 unitId/materialId를 식별에 사용한다. |
| None 개념 오답과 문자열 개념 오답을 함께 정렬 | vendor의 None/str 비교 및 개념 사전 조회로500 발생. null을 유지해 자료·단원별 별도 집계한다. |
| history.raw_score에도 최종 표시 정수 저장 | 상한의 영향을 검토할 수 없는 BE 기록 오류. raw와 제한 후 score를 구분한다. |
| FE mock의 custom 점수 미반영,100/perfect 또는 제출마다 추가 회복 기대 | books/review 계약대로 custom 확정 풀이도 집계하며 표시99, 하루 한 번 기준, 실제 근거 요건을 따른다. 새 FE 필드는 추가하지 않는다. |

테스트는 정상/빈 입력/0점,9·10·19·20 경계,49·50·79·80·99와 정확 HALF_UP,5/15점·날짜 순서·중복 사실·실제 최초 anchor를 검증한다. SQLite/PostgreSQL HTTP 회귀는 같은 code의 두 PDF, 개념 없는 혼합 오답, 같은 날 여러 제출, 미확정/다른 사용자 사실 제외, raw와 표시 분리, 미검증 회독 행으로 보너스가 생기지 않는 경우를 포함한다. 실제 외부 AI 호출은 없다.
