# tutor API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- question 필수, history는 기본 [] 또는 직전 문답 한 쌍만(최대1). 새 질문은 []이며 일반 멀티턴 대화로 누적하지 않는다.
- 쉬운 설명 요청의 history는 사용자의 저장된 원문 문답과 일치해야 한다. 일치하지 않으면 400 BAD_REQUEST. parentId는 서버가 찾은 원문 ID, 원문 근거 범위를 넓히거나 다른 과목으로 바꾸지 않는다.
- pages는 {materialId:string,from:integer,to:integer}[]이며 실제 PDF 범위·소유권 검증. 근거가 전혀 없으면 무근거 응답을 성공으로 만들지 않고 503 AI_FAILED.
- backup basic은 사용자 최신 실패 질문, easy는 최신 실패한 쉬운 설명의 원문을 대상으로 저장 결과를 찾는다. 모호하거나 후보 없으면 AI_FAILED. 임의 AVL 시연 답변을 다른 질문에 반환하지 않는다.
- 동일 질문·자료 버전 결과를 재사용하면 saved:true. 사용자 history의 answer를 신뢰해 근거/정답으로 저장하지 않는다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `POST /api/tutor` · `GET /api/tutor/backup?kind=basic|easy`

| 요청 (`POST`) | 타입   | 필수 | 설명                                                     |
| ------------- | ------ | ---- | -------------------------------------------------------- |
| `question`    | string | 예   |                                                          |
| `history[]`   | array  | 예   | 이전 문답 `{ question, answer, pages }` (지금은 빈 배열) |

| 응답                                 | 타입   | null 가능 | 설명                             |
| ------------------------------------ | ------ | --------- | -------------------------------- |
| `id`                                 | string | 아니요    |                                  |
| `subjectId` · `subjectName`          | string | 아니요    |                                  |
| `question` · `answer`                | string | 아니요    |                                  |
| `parentId`                           | string | 예        | 쉬운 설명이면 원래 답 id (TU-02) |
| `pages[].materialId` · `from` · `to` | –      | 아니요    | 근거 칩                          |
| `saved`                              | bool   | 예        | 저장된 답                        |
| `mockFilled`                         | array  | 예 ([])   | mock 표시용                      |
