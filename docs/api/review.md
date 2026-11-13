# review API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- group 필수 date/unit/count. date는 날짜 내림차순, unit은 과목 ID+단원 순서, count는 오답 횟수 내림차순. 빈 결과는 total=0,groups=[],weakConcepts=[].
- Note.id는 원본 question ID를 나타내는 불투명 ID. history는 {date:string,result:sureWrong|unsureWrong|unsureRight|sureRight,score:number|null}[]이며 날짜 오름차순. score는 서술형만, streak는 0..2, canRetry는 서버 오늘>=next_review_date.
- 오답 또는 애매 정답은 active/review_step=0, 다음 날 복습. 이후 확실 정답이면 1단계와 3일 후 복습, 그때 확실 정답이면 resolved. 예정일 전 반복 정답으로 단계를 건너뛰지 않는다. canRetry=false이면 409 REVIEW_NOT_DUE.
- retry는 choice/text/confidence 규칙과 채점 실패 답안 보존 정책을 quiz와 공유한다. question+복습 예정일 단위 중복 재전송은 같은 결과 반환, 다음 복습 건으로 처리하지 않는다.
- custom-quiz는 소유 과목의 현재 unitId와 범위로 선택한다. 중복 scope 금지. count는 1..50, difficulty는 low/mid/high, weight는 even/weak. even은 단원별 균등 배정, weak는 낮은 숙달도 단원 우선(동점 단원 순서·ID), 단원 내 미풀이 문항 우선. 완료·출제 가능한 범위만 선택. 부족하면 실제 count와 note 반환, 0개면 NO_QUESTIONS. 정답은 미포함.
- relearn은 과목/단원 전체 현재 범위를 재학습했다는 명시적 완료 기록이다. 같은 날짜·단원 중복은 409 ALREADY. round는 저장된 유효 근거 재판정값, relearnedOn은 서버 today. 문항 점수를 임의 상승시키지 않는다.

- custom-quiz에 1회독 단원이 하나라도 포함되면 difficulty=low로 고정하고 note로 설명한다. 응답 difficulty는 low/mid/high이며 문항 화면의 난이도 표시 문자열은 하/중/상이다. **4-1/LV-04, 원문 pp.9,19:** 문제 뽑기의 확정 풀이도 숙달도·취약 문항에 반영한다. FE mock의 점수 미반영을 제품 규칙으로 승격한 이전 문장은 철회한다. daily/more/custom/retry 모두 확정된 풀이만 근거로 사용하며 중복 반영하지 않는다.
- relearn에는 Idempotency-Key가 필수(공통 선택 규칙의 예외, 누락422 INVALID_INPUT). ERD의 direct:SHA256(key) 근거를 저장한다. 같은 키/같은 입력은 최초 결과, 다른 입력은409 IDEMPOTENCY_CONFLICT; 다른 키의 당일 동일 단원 요청은409 ALREADY. 재학습만으로+15 상한을 열지 않는다. 같은 범위의 문제 뽑기 확정 풀이까지 충족해야 해당 날짜 상한15를 적용하며 퀴즈 근거 없이 점수를 올리지 않는다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/review/notes?group=date|unit|count`

| 응답                                                             | 타입   | null 가능 | 설명                      |
| ---------------------------------------------------------------- | ------ | --------- | ------------------------- |
| `group` · `total`                                                | –      | 아니요    |                           |
| `groups[].key` · `label`                                         | string | 아니요    |                           |
| `groups[].items[]`                                               | Note[] | 아니요    | 아래 Note                 |
| `weakConcepts[].subjectId` · `subjectName` · `concept` · `count` | –      | 아니요    | 취약 개념 (오답 2회 이상) |

Note: `id` · `subjectId` · `subjectName` · `unitNo` · `unitTitle` · `concept` · `question` · `page` · `type` · `wrongCount` · `lastDate` · `result` (`sureWrong` · `unsureWrong` · `unsureRight` · `resolved`) · `score` (서술형, null 가능) · `streak` (확실 정답 연속, 2면 해결) · `canRetry` bool · `history` array · `mockFilled` (null 가능)

#### `GET /api/review/notes/:id/question` · `POST /api/review/notes/:id/retry`

| 요청 (`retry`) | 타입   | 필수        | 설명                   |
| -------------- | ------ | ----------- | ---------------------- |
| `choice`       | number | choice만    | 보기 번호, 아니면 null |
| `text`         | string | 단답 · 서술 | 아니면 null            |
| `confidence`   | string | 예          | `sure` · `unsure`      |

- `question` 응답: `noteId` · `type` · `prompt` · `options` · `mockFilled` (null 가능)
- `retry` 응답: `note` (Note) · `correct` bool · `confidence` · `score` (null 가능) · `explanation` · `wrongCountBefore` · `unitChange` `{ unitNo, before{ score, grade }, after{…} }`

#### `POST /api/review/custom-quiz` — 문제 뽑기 (RV-02)

| 요청         | 타입     | 필수 | 설명                   |
| ------------ | -------- | ---- | ---------------------- |
| `subjectId`  | string   | 예   |                        |
| `scopes` | object[] | 예 | `{unitId,from,to,conceptId?}`; 아래 계약 |
| `difficulty` | string   | 예   | `low` · `mid` · `high` |
| `count`      | number   | 예   | 문항 수                |
| `weight`     | string   | 예   | 출제 비중 (위 S10 절)  |

응답: `quizId` · `count` · `difficulty` · `note` (null 가능)

#### `POST /api/review/relearn` — 범위 전체 재학습 (RV-03)

요청: `subjectId` string · `unitId` string (둘 다 필수). 응답: `subjectId` · `materialId` · `unitId` · `unitNo` · `round` · `relearnedOn`


## 다중 자료·개념 범위 (BE PracticeScope·LearningPassInput 채택)

```json
{"subjectId":"subject-id","scopes":[{"unitId":"unit-id","from":1,"to":5,"conceptId":"concept-id"}],"difficulty":"low","count":5,"weight":"weak"}
```

기존 units:number[] 입력은 폐기한다. scopes는 비어 있지 않아야 한다. unitId/from/to 필수, conceptId 생략 시 범위 내 시험 포함 개념 전체를 선택한다. 지정하면 해당 개념과 페이지의 교집합만 출제한다. 여러 개념/비연속 범위는 여러 scope로 전달한다. 실제 문항은 원본 문항 ID로 중복 제거한다. 단원에 속하지 않는 개념, 시험 제외·미학습·quizless 범위, 다른 사용자 단원은 거절한다. materialId는 unitId로 서버가 결정하므로 요청에 중복 전달하지 않는다.

Note와 weakConcepts에는 `materialId`, `unitId`, `conceptId`(개념 없으면 null)를 추가한다. retry.unitChange에도 materialId/unitId를 포함한다. 분류 key는 표시 번호가 아닌 서버가 내려준 불투명 ID를 사용한다. 화면의 unitNo/단원명은 유지할 수 있다. relearn은 선택 단원 전체의 현재 시험 범위 재학습이며 일부 concept만 재학습한 것을 전체 회독으로 기록하지 않는다.
