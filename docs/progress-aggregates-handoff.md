# D-01 진행률 집계 인계

## 완료한 경계

`StudyData`가 서로 다른 세 분모를 계산한다. R-01 `PageSpan` 구간 연산과 R-03
`TaskView`를 재사용하며 페이지마다 set을 만들지 않는다. 스키마, 기록 쓰기, 집중
서비스, 퀴즈 서비스, AI, seed, `app/main.py`는 변경하지 않았다.

| 계산 | 계획 분모 | 완료 근거 |
| --- | --- | --- |
| `progress(day)` / 홈 달성률 | 해당 날짜의 유효 배치마다 TaskView.pages를 합산. 새 학습·보충·페이지가 있는 복습은 각 의무를 별도로 셈 | 같은 배치의 최신 스냅샷을 unit별 task 범위와 교차한 TaskView.donePages |
| `readiness(subject)`의 과목 진도 | 현재 시험 범위의 material별 고유 페이지 합집합 | task_type=study의 실제 완료를 같은 unit의 task 범위와 현재 시험 범위에 교차한 합집합. 보충·복습은 진도 근거에 추가하지 않음 |
| `quiz_progress(day)` / 퀴즈 개방 | 현재 유효한 오늘 배치의 quiz_enabled 시험 범위를 material별로 합집합 | 그 오늘 배치들의 최신 완료 근거만 교차한 합집합. 과거·미래·carried_over·excluded 다른 배치의 완료는 사용하지 않음 |

같은 PDF 안의 중복은 각 계산 경계 안에서 제거하고 다른 PDF의 같은 쪽은 별도로
센다. 예를 들어 같은100쪽의 미완료 새 학습과 완료 보충이 있으면 홈은50%,
퀴즈는100%, 그 두 배치만으로 얻은 과목 새 학습 진도는0이다.

과목 진도는 현재 배치 상태와 학습 사실을 구분한다. carried_over/excluded의 기존
새 학습 완료 사실은 현재 유효한 같은 unit 범위에 한해 남는다. retired unit의
기록을 같은 code의 새 단원에 이전하지 않는다. POST/PATCH의 전체 교체 스냅샷을
다시 읽으므로 정정 전 완료량이 중복으로 누적되지 않는다.

## 범위와 준비도

- 활성 과목의 non-retired, in_scope 페이지 단원만 현재 시험 범위다. 현재 개념이
  있으면 포함된 개념의 합집합을 단원 범위와 교차한다. 개념이 없으면 단원 전체,
  개념이 모두 제외되면0쪽이다. 페이지 없는 단원은 페이지 가중치를 만들지 않는다.
- 과목 진도 분모에는 quizless 자료도 포함한다. 숙달도 가중 평균에는 quiz_enabled이고
  확정된 mastery_score가 있는 단원만 사용한다. 가중치는 위 유효 단원 범위의 페이지 수다.
- 서로 다른 현행 단원의 유효 시험 범위가 같은 material에서 겹치면 과목 진도는
  합집합으로 계산하지만 mastery=null/readiness=0이다. 보관 과목·retired·전부 제외
  단원은 충돌 대상이 아니다. **기존 BE 명세에 손상 데이터 가중 규칙이 없어 계산
  불가로 확정**한 정책이며 임의 평균이나 우선 단원 선택은 하지 않는다.
- progressRate/mastery 표시는 소수 한 자리 HALF_UP이다. readiness는 표시값을
  곱하지 않고 실제 완료비율×원 페이지 가중 숙달도에서 정수 HALF_UP한다.
  예:3496/10000, mastery10은 progressRate35.0, readiness3이다. 표시35.0을
  다시 곱하면 잘못된 readiness4가 된다. 점수 근거가 없으면 mastery=null/readiness=0이다.

## 재사용 인터페이스

`app/domain/progress.py`의 immutable `PageProgress(done_pages, total_pages)`는
음이 아닌 정수만 받는다. `.rate`는 100 상한의 정수 HALF_UP이고 분모0이면0이다.
`.ratio`는 상한1의 Fraction, `.reaches(70)`는 정수 분자·분모로 비교하며 분모0이면
항상 false다. `.remaining_pages(70)`는 max(0, ceil(total*0.7)-done)이다.
`from_ranges(planned, completed)`는 material별 합집합과 교차로 분자·분모를 만든다.

`StudyData.quiz_progress(day)`는 내부 read-model로 다음을 제공한다.

- `donePages`, `totalPages`, `rate`, `subjectRates`: 출제 가능한 페이지 기준 값.
- `open`: 전체 원 비율이70% 이상인지.
- `subjectOpen`: 해당 과목의 분모가 양수이고 전체 또는 해당 과목의 원 비율이70%
  이상인지. 전체가 열려도 과목 분모0이면 해당 과목은 닫힌다.
- `remainPages`: 출제 가능한 전체 분모에서70%까지 남은 페이지.

홈 `quiz_view(day, prog)`는 기존 호출 형태를 유지하지만 전달된 일반 홈 달성률을
판정 근거로 사용하지 않는다. `quiz_today`와 `_check_open`도 같은 quiz_progress를
사용한다. 더 풀기/문제 뽑기와 이미 제출된 세트의 기존 예외는 유지한다.
현재 과목 상세 후속 U-01도 `readiness(subject)`를 재사용해야 한다.

## FE/BE 차이와 후속 책임

- BE는 기존에 홈의 반올림된 일반 rate로 퀴즈를 열었다. 이제 FE는 홈 progress와
  subjectRates를 달성률 표시로만 사용하고, 퀴즈 접근은 서버의 quiz.open,
  openSubjects 및 `/api/quiz/today`의 open/subjects[].open을 읽어야 한다.
  화면에70%가 보여도 실제69.99%는 잠기며 remainPages=1이다.
- 실제 FE `src/api/adapters/today.js`, `src/api/adapters/quiz.js`는 이미 서버 open을
  보존하고 `src/pages/quiz/S8Quiz.jsx`도 s.open으로 잠김을 판정하므로 필드 변경은
  필요 없다. 다만 `src/pages/home/S6Home.jsx`의 QuizGuideBox(현재72행)는 일반
  progress.rate와 퀴즈 remainPages를 같은 문장의 동일 분모처럼 연결한다.
  `src/pages/quiz/S8Quiz.jsx`의 카드 머리(현재40행)와 quiz adapter의 rate 주석도
  이를 '오늘 계획 진행률'이라고 부른다. FE 후속은 달성률/출제 가능한 페이지의
  문구를 분리해야 한다. books adapter의 mastery=null 보존과 S9Books의 null일 때
  수식 숨김은 이번 손상/무점수 응답에 그대로 호환된다. FE 파일은 수정하지 않았다.
- BE의 기존 과목 진도는 단원별 페이지 수 합산과 완료 페이지 set을 사용했고, 개념
  제외·다중 자료 중복·quizless 숙달 가중치를 분리하지 않았다. 현재 범위 합집합으로
  보정했으며 홈 exams와 `/api/books` readiness가 같은 결과를 반환한다.
- 기존 BE quiz 명세의 '오늘 페이지'가 회차별 합산인지 고유 페이지인지 모호했다.
  오늘 출제 가능한 material별 합집합으로 확정하고 `docs/api/quiz.md`에 명시했다.
  문항별 답안 저장 계약 섹션은 변경하지 않았다.
- **D-02:** 날짜별 분모 동결과 과거 daily_plan_totals 소비는 후속이다. 이번
  progress(day)는 조회한 실제 배치로 계산하며 동결을 구현했다고 표시하지 않는다.
- **D-03:** `app/routers/today.py::post_record`의 dayClose/rate70 toast는 아직 일반
  표시 rate를 비교한다. P1은 `PageProgress(donePages, totalPages).reaches(70)`를,
  실제 퀴즈 열림 전이는 별도 quiz_progress의 subjectOpen/open을 사용해야 한다.
  23:30/하루1회 조건과 정정 후 P1 정책은 이번 범위에 추가하지 않았다.
- **Q-07:** `QuizService.ensure_daily`의 실제 문항 선택, 완료 범위 제한, 회독별 난이도와
  실제 문항 수 확정은 후속이다. 이번 quiz_counts는 현재 유효한 모든 task range의
  eligible unit ID를 세고 이미 만들어진 세트의 수를 우선하지만, 미생성 세트는
  기존 단원당5문항 예상값을 유지한다. 접근 gate 보정이 문항 구성 완료를 뜻하지 않는다.
- **S-01/Q-05/B-01:** 저장 점수 확정·회복 정책과 결과 snapshot·회독 생성은 해당
  후속/통합 서비스가 담당한다. 이번 집계는 기존 확정 mastery_score를 읽는다.

## 검증

테스트는 conftest의 SQLite DB와 PostgreSQL 독립 스키마를 사용했다. 로컬 PostgreSQL은
127.0.0.1:54511/team10_test이며 공유 venv 패키지는 변경하지 않았다.

- 순수 비율 계산:22 passed.
- 홈 집계 + 기록 POST/PATCH/session/demo 영향:263 passed.
- 과목 준비도 + TaskView/demo 영향:127 passed; 추가 이중 반올림 경계:4 passed.
- 전체 신규 progress 계약:66 passed.
- 기존 퀴즈 식별자/서술형 실패/데모 제출 영향:230 passed.

최종 영향 범위 명령(`LOCAL_TEST_POSTGRES_URL`은 승인된 위 로컬 DB URL):

```sh
TEST_POSTGRES_URL="$LOCAL_TEST_POSTGRES_URL" .venv/bin/python -m pytest -q \
  tests/test_progress_contract.py tests/test_task_views.py \
  tests/test_record_ranges.py tests/test_record_transactions.py tests/test_record_sessions.py \
  tests/test_today_suggestions.py tests/test_demo_package.py \
  tests/test_focus_api.py tests/test_focus_state.py tests/test_focus_view.py tests/test_focus_concurrency.py \
  tests/test_demo_focus_seed.py tests/test_demo_focus_expiry.py tests/test_demo_next_day.py \
  tests/test_planner_exam_boundary.py tests/test_quiz_identifiers.py tests/test_essay_failure.py \
  tests/test_demo_quiz_flow.py tests/test_books_rounds.py tests/test_demo_state.py \
  tests/test_contract_inventory.py --tb=short
```

최종 결과: **745 passed**, 263.65초. 기존 SWIG deprecation 경고5개 외 실패/skip은 없다.
기존 테스트의 assertion/기대값 보정은 필요하지 않았으며 신규 계약 테스트를 추가했다.
