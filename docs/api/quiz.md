# quiz API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- 경로 subjectId는 사용자 과목 ID 또는 custom/more quiz ID. 응답에 quizId:string을 추가해 제출 대상을 명시한다. POST submit의 quizId는 선택적으로 받되 FE는 GET에서 받은 값을 전달한다. 과목 경로와 quizId 소유 과목 불일치는 404 NO_QUIZ.
- daily 퀴즈는 출제 가능한 오늘 페이지 진행률이 전체 또는 해당 과목 70% 이상일 때 열림. 분모 0이면 닫힘. 잠긴 GET/submit은 403 QUIZ_LOCKED. 결과 재조회는 열림 조건과 무관.
- **QZ-01·06·07·09, 원문 pp.10,18:** 완료/부분 완료 페이지까지만 출제. 평일은 전날 취약 문항, 토요일은 월~금 오답, 일요일은 토요일 재풀이 중 재오답+토요일 새 범위 오답. 퀴즈 없는 날 취약 문항은 다음 과목 퀴즈로 넘긴다. 미풀이 문항은 다음날 퀴즈에 합친다.
- 1회독은 low 고정,2회독 이후는 사용자가 low/mid/high 선택. GET 문항 요청에 difficulty 쿼리 추가가 필요하며 AI가 임의 선택하지 않는다. 기존 세트와 다른 난이도 요청 시 진행 답안을 덮어쓰지 않는다.
- GET 문항에는 정답·해설을 반환하지 않는다. choice/ox는 화면 type=choice와 options:string[]. choice는 0 기반이며 범위 밖은 422 INVALID_INPUT. short/essay는 text:string; 답을 비워 제출할 때는 빈 문자열을 명시한다.
- answers는 세트의 모든 questionId를 정확히 한 번 포함, 다른 문항/중복/누락은 422 INVALID_INPUT. confidence는 sure/unsure 필수. confidence 누락은 400 CONFIDENCE_REQUIRED. **4-3/QZ-06, 원문 pp.10,18:** 단원당 새5문항+오답 전부이며 오답 개수 제한이 없다. 임의100문항 상한을 철회한다. 공통 배열1000개 제한도 이 answers에는 적용하지 않는다.
- 제출 1단계: 세션 잠금 후 답안·확신도·본문 해시를 저장하고 커밋. 제출 2단계: AI 외부 호출. 제출 3단계: 성공한 채점을 잠금 아래 확정하고 숙달도·취약 문항·history를 같은 트랜잭션으로 한 번만 반영.
- 실패/25초 초과는 503 AI_FAILED, **답안은 보존**하고 quiz grading_status=failed/processing 유지. is_correct/essay_score는 미확정이면 null, 숙달도 미반영. 성공한 일부 문항의 채점은 재사용할 수 있으나 세트 확정 전 결과/집계는 공개하지 않는다.
- 동일 quizId+동일 답안 재제출은 진행 중이면 503 AI_FAILED(처리 중 안내), failed면 미완료 채점만 재시도, completed면 저장한 동일 결과 200. 확정 또는 처리 중 답안을 바꾸면 409 QUIZ_FINALIZED. 복구용 새 자동 의무 퀴즈를 만들지 않는다.
- GET result는 제출 전 404 NO_RESULT, 답안 저장 후 미완료는 503 AI_FAILED, 완료만 200 결과 스키마. FE 재시도는 기존 answers와 quizId 유지; 별도 Job 폴링 없음.
- savedGrading:true는 외부 AI 금지. 문제·답안 해시와 일치하는 검증 결과만 사용. 다른 답안에 DS3-06의 70점을 고정 적용하지 않는다. 해당 결과 없으면 AI_FAILED, 답안은 보존.
- 객관식은 서버의 정답 보기 인덱스로 비교, 단답은 앞뒤/연속 공백 정규화·영문 소문자화 후 비교(문장부호는 보존), 서술형은 유효한 0..100 점수에서 85 이상 정답. 실패를 오답 0점으로 변환하지 않는다.
- **QZ-11, 원문 pp.11,18:** 더 풀기는 같은 단원 미풀이5문항을 제공한다. 이전 초안의1~4개 부분 제공 규칙을 철회한다. 5개 미만이면 세트를 생성하지 않고 NO_QUESTIONS로 준비 부족을 알린다. 충분한 사전 문항 준비가 요구사항이며 단원당10개 이상 생성 여부는 p.22의 AI 확인 항목이다. 동일 일자/단원 재요청은 기존 생성 세트 반환. 문항 부족 자체로 무제한 AI 생성을 시작하지 않는다.
- 완료 결과의 items/summary/unitChanges/bookChanges는 같은 확정 스냅샷. 이후 학습이 바뀌어도 과거 before/after를 재작성하지 않는다. more 배열은 완료된 자식 세트만 포함하고 재귀 순환 금지.

## 문항별 저장 계약 추가 (와이어 S8·S8-3, NF-02)

기존 FE는 마지막 bulk submit만 호출한다. 와이어의 [다음] 답 저장/서술형 비동기 채점을 만족시키기 위해 다음 API가 추가로 필요하다. 이는 공통 Job/202 API와 별개다.

`PUT /api/quiz/:quizId/answers/:questionId`

- header X-Anonymous-Id, body {choice:integer|null,text:string|null,confidence:sure|unsure,revision:integer}. 최초 revision=0, 이후 서버가 반환한 revision 사용.
- 200 data:{quizId,questionId,saved:true,revision:integer,gradingStatus:pending|processing|completed|failed}. 점수·정답·해설은 반환하지 않는다. 저장 후 다음 문제로 진행 가능.
- 문항별 답안 저장을 먼저 커밋한 뒤 서술형 채점 작업을 큐에 넣는다. 마지막 submit은 저장된 전체 답안을 봉인하고 채점 완료분을 재사용한다. 기존 answers bulk도 호환 입력으로 검증/저장하되 동일 attempt를 중복 생성하지 않는다.
- 같은 revision+동일 입력 재전송은 같은 응답; 같은 revision+다른 입력은409 ANSWER_CONFLICT. 새 답안을 허용하는 미제출 상태에서는 이전 채점 작업의 answer revision을 무효화해 오래된 결과가 덮어쓰지 못하게 한다. 제출 완료 후 변경은409 QUIZ_FINALIZED.
- GET 문항 응답에 quizId와 savedAnswers:[{questionId,choice,text,confidence,revision}]를 추가한다. 새로고침 후 풀이 복구. 정확한 정답/채점 결과는 완료된 결과 조회에만 공개한다.
- AI 실패는 답 저장 성공 자체를 취소하지 않는다. 마지막 submit의503 AI_FAILED 및 재시도 규칙 유지. 문제 진행 API와 최종 결과 API를 구별한다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/quiz/today` — S8-1 카드

| 응답                                      | 타입     | null 가능 | 설명                                       |
| ----------------------------------------- | -------- | --------- | ------------------------------------------ |
| `date`                                    | string   | 아니요    |                                            |
| `rate`                                    | number   | 아니요    | 오늘 진행률                                |
| `open` · `openRate`                       | –        | 아니요    |                                            |
| `subjects[].subjectId` · `name` · `color` | string   | 아니요    |                                            |
| `subjects[].count`                        | number   | 아니요    | 문항 수                                    |
| `subjects[].rate`                         | number   | 아니요    | 과목별 오늘 진행률                         |
| `subjects[].open`                         | bool     | 아니요    | 전체 70% 또는 과목 70%                     |
| `subjects[].hasQuestions`                 | bool     | 아니요    | 문항 준비됨                                |
| `subjects[].lines`                        | string[] | 예 ([])   | 구성 줄 (예 `"3단원 새 문제 5 (p.72–88)"`) |
| `subjects[].round`                        | string   | 예        | 회독 (`1회독` …)                           |
| `subjects[].difficulty`                   | string   | 예        |                                            |
| `subjects[].essayCount`                   | number   | 예        |                                            |
| `subjects[].result`                       | object   | 예        | 이미 풀었으면 `{ correct, total }`         |

#### `GET /api/quiz/:subjectId` (`?saved=1`) — 문항 세트

`:subjectId`는 과목 id 또는 문제 뽑기 · 더 풀기로 만든 세트 id예요.

| 응답                     | 타입     | null 가능 | 설명                                   |
| ------------------------ | -------- | --------- | -------------------------------------- |
| `saved`                  | bool     | 예        | 저장된 결과                            |
| `subjectId` · `name`     | string   | 아니요    |                                        |
| `custom`                 | bool     | 예        | 문제 뽑기 · 더 풀기 세트               |
| `resultFor`              | string   | 예        | 풀고 나서 볼 결과 (없으면 `subjectId`) |
| `round` · `difficulty`   | string   | 아니요    | 1회독은 하 고정 (QZ-09)                |
| `composition`            | string   | 아니요    | 구성 한 줄                             |
| `questions[].id`         | string   | 아니요    |                                        |
| `questions[].type`       | string   | 아니요    | `choice` · `short` · `essay`           |
| `questions[].tag`        | string   | 예        | 예 `"어제 오답"`                       |
| `questions[].unitNo`     | number   | 아니요    |                                        |
| `questions[].page`       | string   | 아니요    | 근거 페이지                            |
| `questions[].prompt`     | string   | 아니요    |                                        |
| `questions[].options`    | string[] | choice만  | 보기 (OX도 choice로 보기 2개)          |
| `questions[].mockFilled` | array    | 예 ([])   | mock 표시용                            |

#### `POST /api/quiz/:subjectId/submit` — 제출 · 채점

| 요청                   | 타입   | 필수        | 설명                                |
| ---------------------- | ------ | ----------- | ----------------------------------- |
| `answers[].questionId` | string | 예          |                                     |
| `answers[].choice`     | number | choice만    | 고른 보기 번호 (0부터), 아니면 null |
| `answers[].text`       | string | 단답 · 서술 | 아니면 null                         |
| `answers[].confidence` | string | 예          | `sure` · `unsure`                   |
| `savedGrading`         | bool   | 선택        | 저장된 채점으로                     |

응답 = 아래 결과 (`GET /api/quiz/:subjectId/result`와 같은 모양)

#### `GET /api/quiz/:subjectId/result` — 결과 (S8-2 · P3)

| 응답                                                                    | 타입   | null 가능 | 설명                                                                                  |
| ----------------------------------------------------------------------- | ------ | --------- | ------------------------------------------------------------------------------------- |
| `subjectId` · `name`                                                    | string | 아니요    |                                                                                       |
| `essayGrading`                                                          | string | 예 (ai)   | `ai` · `saved`                                                                        |
| `summary.total` · `correct` · `sureCorrect` · `unsureCorrect` · `wrong` | number | 아니요    |                                                                                       |
| `items[].questionId`                                                    | string | 아니요    |                                                                                       |
| `items[].no` · `unitNo`                                                 | number | 아니요    |                                                                                       |
| `items[].type` · `tag` · `prompt` · `page`                              | string | tag 예    |                                                                                       |
| `items[].confidence`                                                    | string | 아니요    | `sure` · `unsure`                                                                     |
| `items[].correct`                                                       | bool   | 아니요    | 서술형도 서버 판정 (85점 이상)                                                        |
| `items[].score`                                                         | number | 예        | 서술형 0~100                                                                          |
| `items[].feedback`                                                      | object | 예        | 서술형 `{ good, miss }`                                                               |
| `items[].explanation`                                                   | string | 예        | 해설 (v2는 모든 문항)                                                                 |
| `items[].evidence`                                                      | object | 예        | 근거 원본 `{ materialId, page, quote }` (QZ-13)                                       |
| `items[].mockFilled`                                                    | array  | 예 ([])   | mock 표시용                                                                           |
| `unitChanges[].unitNo` · `title`                                        | –      | 아니요    |                                                                                       |
| `unitChanges[].before`                                                  | number | 예        | 퀴즈 전 점수 (없으면 '퀴즈 전')                                                       |
| `unitChanges[].after` · `grade`                                         | –      | 아니요    | 등급 `stable` · `shaky` · `risk`                                                      |
| `unitChanges[].note`                                                    | string | 예        |                                                                                       |
| `bookChanges[]`                                                         | array  | 예 ([])   | P3: `unitNo` · `title` · `before{ score, grade, blocks }` · `after{…}` · `recovering` |
| `wrongAdded[]`                                                          | array  | 아니요    | 오답 노트에 들어간 문항 `{ no, reason }`                                              |
| `unitNo`                                                                | number | 예        | 더 풀기 결과면                                                                        |
| `moreUnitChange`                                                        | object | 예        | 더 풀기 이해도 변화                                                                   |
| `more[]`                                                                | array  | 예 ([])   | 더 풀기 결과 (같은 모양) — 원래 결과 아래에 붙여요                                    |

#### `POST /api/quiz/:subjectId/more` — 더 풀기 (QZ-11)

| 요청     | 타입   | 필수 |
| -------- | ------ | ---- |
| `unitNo` | number | 예   |

응답: `quizId` string (→ `/quiz?subject=quizId`로 풂) · `count` number
