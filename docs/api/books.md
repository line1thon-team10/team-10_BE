# books API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- 하나의 지속 study_block은 책 하나. 이월한 여러 task를 중복 책으로 세지 않는다. units 정렬은 현재 sort_order, 각 blocks는 최초 배치 순서.
- grade는 risk/shaky/stable 또는 null. perfect는 이번 API enum에 포함하지 않으며 점수100을 만들지 않는다. 회독 횟수는 round로만 표시한다.
- 진행률은 현재 시험 범위 내 학습한 고유 페이지/시험 범위 페이지. 반복 학습으로 100%를 초과하지 않는다. 전체 페이지 0이면 progressRate=0.
- 숙달도는 현재 출제 가능한 단원 중 확정 점수가 있는 단원을 페이지 수로 가중 평균. 점수 있는 단원이 없으면 mastery=null, readiness=0. readiness=반올림(progressRate*mastery/100).
- 단원 점수: 확실 정답 1, 애매 정답 0.5, 오답 0; 난이도 low/mid/high 가중치 1/1.5/2. 전체 가중 정답률 A, 최근 최대20건 B에 대해 100*(alpha*A+(1-alpha)*B). 최근 건수 <10이면 alpha=.5, <20이면 .4, 그 외 .3. 최대99.
- 80미만 경험 후 회복 모드. 회복 모드에서는 같은 날짜 확정 풀이를 묶어 전일 점수 대비 상승을 최대5(해당 날짜 해당 범위 전체 재학습과 문제 뽑기 풀이 근거가 모두 있으면15)로 제한, 하락은 그대로. 날짜/answered_at/attempt ID 순으로 계산하고 HALF_UP 정수 반올림. risk<50, shaky<80, stable>=80.
- 부분/전체 학습과 회독은 별개다. 회독은 ERD learning_passes의 유효 근거로 판정하며 단순 기록 호출 횟수로 증가시키지 않는다. 제목/태스크 예상시간 편집은 회독을 늘리지 않는다.

- **4-2/LV-03, 원문 pp.10,18:** 1회독 완료 후80점 미만이면 틀린 개념의 페이지만 그 과목 다음 공부일에 보충 태스크로 추가한다. 보충은 진도율 제외, 삭제 금지, 완료 시2회독 자격. 회복 중 stable 승격은 보충 완료와 서로 다른2일 이상의 결과를 모두 요구한다.
- 하루 등급 상승은 risk→shaky 또는 shaky→stable 한 단계만 허용한다. risk에서 바로 stable이 되는 계산은 표시 점수를79로 제한한다. 회복 상한과 등급 상한 중 더 낮은 것을 적용한다.
- 오늘 완료한 블록은 오늘 해당 단원 퀴즈 확정 전에는 done, 확정 후 단원 등급으로 표시한다. 부분 학습은 doing, 미시작은 planned. recentDays와 books에 같은 규칙 적용.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/books`

| 응답                                              | 타입   | null 가능 | 설명                                         |
| ------------------------------------------------- | ------ | --------- | -------------------------------------------- |
| `subjects[].subjectId` · `name` · `color`         | string | 아니요    |                                              |
| `subjects[].total`                                | number | 아니요    | 계획한 책(태스크) 수                         |
| `subjects[].collected`                            | number | 아니요    | 쌓인 책 수 (예정 제외)                       |
| `subjects[].units[].no` · `title` · `from` · `to` | –      | 아니요    |                                              |
| `subjects[].units[].grade`                        | string | 예        | `stable` · `shaky` · `risk` · null (퀴즈 전) |
| `subjects[].units[].score`                        | number | 예        | 0~99                                        |
| `subjects[].units[].round`                        | number | 예 (1)    | 회독                                         |
| `subjects[].units[].note`                         | string | 예        | 단원 메모                                    |
| `subjects[].units[].blocks[].state`               | string | 아니요    | 블록 상태                                    |
| `subjects[].units[].blocks[].supplement`          | bool   | 예        | 보충 학습                                    |
| `subjects[].units[].blocks[].task`                | object | 예        | `{ title, from, to, doneTo }`                |
| `readiness[].subjectId`                           | string | 아니요    |                                              |
| `readiness[].readiness` · `target`                | number | 아니요    | 준비도 % · 목표선 %                          |
| `readiness[].targetLabel`                         | string | 아니요    | 예 `"A+"`                                    |
| `readiness[].progressRate`                        | number | 예        | 진도율 %                                     |
| `readiness[].mastery`                             | number | 예        | 숙달도 % (null이면 식을 숨김)                |
| `readiness[].studiedPages` · `totalPages`         | number | 예        | 진도율 분자 · 분모                           |


## 다중 자료 식별

`subjects[].units[]`에 `id`(unitId), `materialId` string을 필수로 추가한다. no는 자료 내 표시 순번이다. blocks[]에는 `id`(영속 책 ID)를 포함하고, blocks[].task가 있으면 `id`(taskId), `pages`, `ranges`([학습 범위 형식](study.md))를 함께 반환한다. 복합 범위의 from/to/doneTo는 null이다. FE는 unitId/blockId로 렌더링·선택하고 서버 pages를 사용한다. 다른 PDF의 같은 단원 번호·페이지를 합산 중복 제거하지 않는다.
