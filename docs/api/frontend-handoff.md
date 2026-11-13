# FE 전달용 API 변경 안내 — 2026-11-13

[PR #3](https://github.com/line1thon-team10/team-10_BE/pull/3)의 **FE 연동 계약**입니다. FE 기본 계약을 유지하되 요구사항/와이어 충돌은 기존 BE 명세로 보완하고, BE에도 오류/누락이 있으면 새로 정의했습니다. 아래 변경은 문서에 반영되었으며 **서버 구현·배포 완료를 의미하지 않습니다**. FE 런타임은 이 PR에서 수정하지 않았습니다.

기준 FE: `c8e250d9484a03eac005e12c24d1b703fc105626`. 기준 BE: main `3475cf204ac726f9c96d44bbc687ea8253f8c48e` 및 저장된 `schema/openapi-0.7.0.json`. 아래 '기존 BE 초안'은 이 PR의 교정 전 Markdown을 뜻하며, 'BE 0.7'은 기존 OpenAPI를 뜻합니다. PDF 파일명 v3/본문 v2의 차이와 원문 확인 범위는 [출처 기록](source-audit.md)에 있습니다.

## 유지하는 계약

`/api`, `{success,data,error}`, `X-Anonymous-Id`, FE 요청 제한30초, PDF만 작업 폴링, 기존 FE71개 method/path를 유지합니다. 새 경로5개를 추가해 총76개입니다. 공통202 Job/AI 회고/로그인 기능을 추가하지 않습니다. DB 필드명을 FE에 그대로 노출하는 방식으로 전환하지 않습니다.

## 충돌·수정 근거와 담당 작업

| ID / 분류 | 기존 정의와 문제 | 원문 / 기존 BE 대조 | 결정과 FE 작업 | BE 작업·검증 기준 |
|---|---|---|---|---|
| C01 FE 누락 + BE 초안 오류 | FE files[]는 이미 여러 PDF 지원. 그러나 units 조회는 subjectId만, more/relearn은 unitNo만 사용. BE 초안은 PDF1개로 축소 | PDF-01 p.16, S3 / BE material별 units·ExtraQuizInput.unitId | 자료별 materialId, 단원별 unitId를 보존. 번호는 표시용 | 자료별 소유권/페이지 검증; PDF2개 각각 1단원을 구분 |
| C02 FE 누락 → BE 채택 | concepts:string[]/단원 bool로 개념별 범위 저장 불가 | PDF-08 p.16, S4 / BE ConceptInput·UnitsReplace | concepts를 id/title/from/to/inExam 객체로 변경. 단원/개념 체크 저장, 승인 후 편집은 비교 화면 | 구조 변경 retire·이력 보존. 개념 모두 제외·범위 중첩·타인 ID 검증 |
| C03 FE 표현 부족 → BE 채택 | toTask가 to-from+1 계산, 단일 toPage로 복합 범위 기록 불가 | PDF-01·08 / BE PageTaskRange | 서버 pages/ranges 사용, partial은 completedRanges; 단일 범위 toPage 호환 | 자료별 고유 페이지 집계, 겹침·0쪽·전체 완료·정정 검증 |
| C04 FE 누락 + BE 초안 교정 | 마지막 bulk submit만 저장. 이전 BE 초안은 답 저장 응답에 gradingStatus 노출 | S8·S8-3, NF-02 / BE AnswerInput·AnswerSaved | [다음]에서 문항 저장, savedAnswers로 복구. quizId 필수, questions[].id는 세트 항목 ID | 저장 후 비동기 채점, revision 충돌·오래된 결과 무효화; 채점 상태/정답 미노출 |
| C05 FE 입력 누락 | 일반 퀴즈 difficulty 입력 없음, more는 unitNo만 | QZ-09·11 p.18 / BE ExtraQuizInput | 2회독 이후 difficulty 선택, more.unitId. 제출에 quizId 보존 | 1회독 low, 진행 세트 난이도 변경 거절; 더 풀기 정확히5개 |
| C06 FE/BE 초안 부족 | 풀이 중 page 노출, 결과 근거는 인용/페이지만으로 위치 표현 불충분 | S8, QZ-13 p.18 / BE의 근거 분리 의미 채택·rects 형식 새 정의 | 풀이 중 page/근거 제거. 결과 evidence의 rects 또는 인용문/페이지 fallback | 실제 근거만 반환. rects=[] 허용, 임의 위치 생성 금지 |
| C07 FE 누락 → BE 채택 | custom units:number[]로 다중 PDF/개념 범위 선택 불가 | RV-02, PDF-08 / BE PracticeScope | custom.scopes[{unitId,from,to,conceptId?}], relearn.unitId | 미학습/제외/quizless 범위 거절, 문항 중복 제거 |
| C08 FE/BE 초안 보완 | 빈 body로 '마지막 후보' 적용하면 다중 탭 혼선. BE 초안30분 만료는 원문 근거 없음 | PDF p.13 모든 변경 preview+confirm / BE proposal 승인 의미 | 모든 승인에 previewId, 후속 질문에 requestId. stale이면 재조회 | 후보 종류/소유권/날짜/의존 버전 검증, 같은 후보 승인 중복효과 없음; 시간30분 제한 철회 |
| C09 FE 누락 + BE 초안 오류 | 일정 삭제/voice만 있고 시간 수정 없음. 삭제는 즉시 반영 | S11, PDF p.13 / BE ScheduleEditSingleInput·FutureInput | 수정·삭제 미리보기, once/future 선택, 원래 회차 ID·revision 전달 | 이전 회차 보존·future 규칙 분리·예외 변화 표시, 승인 시 원자 적용 |
| C10 FE 축소 + BE 초안 오류 | 신규 과목 버튼 비활성, BE 초안도 미지원으로 확정 | PL-10·11 pp.13,17, S13·S14 / BE SubjectInput·SubjectAddChangeInput | POST subjects→해당 draft 온보딩→자료→S5 승인. 버튼 활성화는 구현 제공 후 연동 | 활성화 전 기존 계획 불변; 승인 후 남은 전체 재배분, 취소해도 기록 보존 |
| C11 FE 축소 + BE 초안 오류 | rebuild를 항상501로 정의 | PDF p.13, S14 / BE RegenerateChangeInput | rebuild 후보→S5-2 비교→resplit/apply; 현재 계획 즉시 교체 금지 | 기록·점수·책·보충 보존, 남은 범위만 재생성 |
| C12 FE/BE 초안 누락 | additions는 minutesPerDay만 받음 | S13 일일 시간 또는 총 페이지 목표 / 새 필드 totalPages 정의 | minutesPerDay/totalPages 중 하나, summary.goalType 표시 | 목표 방식 검증, 배치 불가능·leftPages>0이면 승인 거절 |
| C13 BE 초안 오류 | more30을30분 추가로 취급, 목표 변경 적용일 누락 | PDF p.13 / BE ExtraTimeChangeInput 의미 | more30 경로 유지, 실제 앞당긴 태스크/시간 표시 | 다음 예정 태스크 이동, 목표 가중치는 내일부터; 날짜/순서 검증 |
| C14 BE 초안 오류 | OCR 전부 quizless, 잔여15분 미만 허용 | PDF-07/PL-02 p.16 | OCR와 quizless를 따로 표시, 임의 시간 예외 UI 불필요 | 저품질 OCR만 제외; 모든 태스크15~60분, 불가 시 실패 |
| C15 BE 초안 오류/누락 | 남은 날 평균 밀림 기준, 마감 박스는 전부 기록 시에만 | RC-02 p.12, BK-03 p.11 | 서버 replan/box 판정 사용, P2 enabled 보존 | 과목 전체범위/태스크날수×.5 올림(140/8→9); (전부 기록 OR23:30) AND70% |
| C16 FE mock 가정 + BE 초안 오류 | custom 점수 미반영을 계약으로 채택, 재학습만으로+15 | LV-04 pp.9,19; LV-03 p.10 | 결과 변화 표시, relearn 재시도 키 유지 | 재학습+같은 범위 custom 확정 풀이 시 상한15; stable 회복은 보충+서로 다른2일 |
| C17 BE 초안 누락/오류 | 주말 오답 구성 누락, 임의 문항 상한, 더 풀기1~4개 허용 | QZ-06·07·11 pp.10–11,18 | 고정 문항 수 가정 제거, 준비 부족 안내 | 새5+오답 전체, 주말/이월 규칙; more 0/1/4개 실패·5개 성공 |
| C18 BE 초안 집계 오류 | 홈/월간 달성률과 과목 진도를 같은 고유 페이지로 계산 | S6·S15·S15-1, PDF p.12 | 서버 집계 표시; 화면에서 보충을 일괄 제외하지 않음 | 달성률은 보충 포함, 과목 진도·월간 과목비율은 새 범위만 |
| C19 FE/BE 누락 | 목표 null만으로 확인 추정, 빈 firstExam 접근, 미수집 카드 non-null 표기 | S1의4칸 확인, 빈 사용자 상태 / 확인 상태 새 정의 | confirmedFields/done 사용, firstExam/examDate/dday null 처리 | '없음' 확인과 미응답 분리; 빈 과목 done=false |
| C20 FE 문서/BE 초안 내부 모순 | resplit before/after 표는 array인데 mock·예시는 분 숫자 | FE S5-2 실제 사용 / 새 타입 정의 | before/after는 integer 분; 화면 계산 유지 | 표/응답 일치, 실제 태스크 합계 사용 |
| C21 BE 초안 누락/근거 없는 제한 | 50MiB·25초·일률 배열 제한을 요구사항처럼 고정 | NF-04 p.21, PDF p.22 운영/AI 확인 항목 | FE30초 유지, 한국어 error.message 표시 | 운영 제한 별도, JSON 오류1회 재시도, 실패 답안 보존 |
| C22 BE 초안 누락 | 속도 보정 조건/알림 식별 부족, 시험 당일 자동 보관 위험 | PL-07 p.12, P4, S14 | calibrationId 보존·전달, P2→P4 순서 유지 | 3일·20%·과목당하루1회, 시험 다음 날 보관 |

## 추가 경로 5개

| Method | Path | 용도 |
|---|---|---|
| PUT | `/api/quiz/:quizId/answers/:questionId` | 문항별 답 저장 |
| PATCH | `/api/schedules/:id/preview` | 회차 시간 수정 미리보기 |
| PATCH | `/api/schedules/:id` | 시간 수정 승인 |
| DELETE | `/api/schedules/:id/preview` | 삭제 미리보기 |
| POST | `/api/subjects` | 신규 draft 과목 생성 |

기존 `DELETE /schedules/:id`는 승인용으로 변경됩니다. 기존 `PUT /units`, `PATCH /materials/:id/units`는 승인 계획이 있으면 미리보기, 같은 경로의 `{previewId}` 요청은 적용입니다. 상세 요청·응답/오류는 각 도메인 문서가 기준입니다.

## FE 수정 위치

아래 경로는 위 고정 버전의 FE 저장소 기준입니다. 새 필드를 adapter가 버리지 않도록 API 호출과 함께 수정해 주세요.

| 수정 위치 | 필요한 변경 | 명세 |
|---|---|---|
| `src/api/plan.js`, `adapters/plan.js`, S3Upload/S3_2Toc/S4Units | materialId 전달, concepts 객체, unitId, applied/structure 분기, 범위 변경 비교 | [자료](material.md) |
| `src/api/plan.js`, `adapters/plan.js`, S5Review/S5_1Resplit/S5_2Compare | previewId/requestId 보존·전달, draftSubjectIds, before/after 정수 | [계획](plan.md) |
| `src/api/today.js`, `adapters/today.js`, S7Record/S12Study | pages/ranges, completedRanges, calibrationId, P2 enabled | [기록](study.md), [집중](focus.md) |
| `src/api/quiz.js`, `adapters/quiz.js`, S8Quiz/S8_2Result/P3BookColor | quizId·항목 ID·savedAnswers·difficulty, [다음] 저장, 풀이 중 page 제거, 결과 근거 | [퀴즈](quiz.md) |
| `src/api/review.js`, `adapters/review.js`, S10Review | scopes/unitId, 재학습 요청 키, 결과 점수 반영 | [복습](review.md) |
| `src/api/adapters/books.js`, S9Books | unitId/materialId/blockId, 서버 pages | [책장](books.md) |
| `src/api/schedule.js`, `adapters/schedule.js`, CalendarTab | 회차 ID/revision, 수정·삭제 preview, slots, previewId | [일정](calendar.md) |
| `src/api/manage.js`, `adapters/manage.js`, S14Manage | draft 과목 생성, goalType/totalPages, rebuild 비교, 실제 more30 이동량 | [관리](manage.md) |
| `src/api/onboarding.js`, `adapters/onboarding.js`, S1Schedule | subjectId 컨텍스트와 confirmedFields | [온보딩](onboarding.md) |
| `src/api/adapters/demo.js` | firstExam=null 분기 | [세션](session.md) |
| `src/mocks/handlers/*` | 변경 필드/오류/빈 상태를 서버 명세와 동일하게 교체 | [수용 기준](acceptance.md) |

## 연동 예시

예시는 success/data 바깥 틀을 생략했습니다. 아래 ID 문자열은 설명용입니다.

```json
{"quizId":"quiz-id","questionId":"quiz-item-id","saved":true,"revision":1}
```

이 값은 `PUT /api/quiz/quiz-id/answers/quiz-item-id`에 `{choice:0,text:null,confidence:"sure",revision:0}`을 저장한 응답입니다. [다음]은 이 성공 응답 후 진행하고, 새로고침은 GET 세트의 savedAnswers로 복원합니다. 최종 제출은 기존 answers 전체와 quizId를 함께 보냅니다. AI 실패503에도 답안을 지우지 않고 같은 세트·답안으로 재시도합니다.

```json
{"previewId":"preview-id"}
```

계획·자료·일정 승인에는 비교 화면에서 받은 ID를 그대로 전달합니다. 409 PREVIEW_STALE이면 새 미리보기를 받아 사용자가 다시 확인합니다. 자동으로 다른 후보를 적용하지 않습니다.

## 남은 연동 확인과 검증 범위

OCR 품질 임계값, AI의 실제 위치 출력/중간 자료 처리, 시연 데이터와 운영 업로드 한도·AI 처리 예산은 별도 연동 확인 사항입니다. 필드 모양과 실패 처리는 정했지만 AI 품질이나 배포 가능 여부까지 검증한 것은 아닙니다. PPT는 현재 PDF 계약에 포함하지 않습니다. 계획 우선순위의 항별 상세 수치 함수도 BE/AI 계산 정책으로 남으며 외부 응답 형식을 바꾸지 않습니다.

문서 검사 대상은 경로 누락/중복, 출처 메타데이터, 로컬 Markdown 링크입니다. 71 FE+5 추가 경로를 확인합니다. [수용 기준](acceptance.md)은 구현 후 실행할 목록이며, 이번 PR에서 FE 화면/BE 런타임/AI 통합 테스트를 통과했다는 뜻이 아닙니다. `schema/openapi-0.7.0.json`은 대조용 구버전이므로 현재 계약으로 코드 생성하지 마세요.
