# material API

## 구현 규칙 (아래 원문 필드 표보다 우선)

- v1 업로드는 PDF만, 최대 50 MiB. MIME과 실제 PDF 구문 검사; 415 NOT_PDF, 413 FILE_TOO_LARGE. 암호로 잠겨 분석 불가하면 400 BAD_REQUEST. 파일 저장명은 서버가 생성한다.
- 과목당 현재 자료 1개. 교체는 기존 분석 작업을 취소하고 자료 구조를 새 버전으로 만든다. 기존 기록/문항의 과거 참조는 보존한다. 승인된 계획에 영향을 주는 구조 편집은 409 PLAN_CHANGE_REQUIRED; 관리 변경 흐름으로 진행한다.
- 접수는 200 {materialId,jobId}; DB 작업 생성과 파일 저장 실패를 정리한 뒤 반환. 동일 사용자·subjectId·파일 SHA256의 완료 작업은 재사용, 진행 작업은 같은 jobId 반환, 실패 작업은 새 시도로 재접수.
- status는 running/done/failed. current는 0 기반, steps.length 미만. done이면 progress=100, failedStep/reason=null; failed이면 failedStep=current와 한국어 reason. running progress는 0..99. 이전 구조 버전 작업은 현재 단원을 덮어쓰지 않는다.
- OCR 성공과 분석 실패를 구별한다. OCR 성공은 단원 저장, ocr=true·quizless=true. 분석 실패만 수동 목차 입력으로 안내. 업로드 파일명으로 OCR/성공 여부를 결정하지 않는다.
- 수동 목차는 subjectId/text로 미리 파싱한 units를 반환한다. PUT /units로 확정한다. 신규 수동 단원의 id는 생략 허용하며 서버가 발급(원문 필수 표보다 우선). no는 자료 내 유일한 양의 정수, title은 비어 있지 않음, 1<=from<=to<=실제 페이지 수. 현재 단원 범위는 겹치지 않는다.
- 자료 없는 수동 목차는 400 NO_MATERIAL. 목차/ OCR 단원은 quizless를 서버가 결정하며 클라이언트 false로 해제 불가. difficulty=null은 AI 추정 없음으로 허용.
- PUT /units는 전체 현재 단원 목록 교체. 빈 배열은 400 NO_UNITS. 제목/순서 변경은 ID 유지, 페이지/개념 의미 변경은 retire 후 신규 ID. 과거 이력을 새 단원으로 재연결하지 않는다.
- evidence는 string[]이며 실제 근거 설명만 반환, 좌표 상자는 이번 버전에서 제공하지 않는다. file은 {name:string,pages:integer} 또는 null.
- GET file은 정규화된 PDF, 없으면 404 NO_FILE. 경로/스토리지 키는 반환하지 않는다. 근거 페이지는 이 파일의 실제 페이지와 일치해야 한다.

## 요청·응답 필드

FE 초안의 필드 표를 기준으로 하며 타입·기본값은 common.md, 변경 사항은 decisions.md를 적용한다.

#### `GET /api/materials` — 과목별 자료

| 응답                               | 타입   | null 가능 | 설명                          |
| ---------------------------------- | ------ | --------- | ----------------------------- |
| `subjects[].id` · `name` · `color` | string | 아니요    |                               |
| `subjects[].files[].id`            | string | 아니요    |                               |
| `subjects[].files[].name`          | string | 아니요    |                               |
| `subjects[].files[].pages`         | number | 예        |                               |
| `subjects[].files[].status`        | string | 아니요    | `running` · `done` · `failed` |
| `subjects[].files[].jobId`         | string | 예        | 분석 중이면                   |
| `subjects[].files[].quizless`      | bool   | 예        | 이 파일 범위는 퀴즈 없음      |
| `subjects[].files[].ocr`           | bool   | 예        | 글자 인식으로 분석한 스캔본   |

#### `POST /api/materials` — 업로드 (FormData)

| 요청        | 타입   | 필수 | 설명 |
| ----------- | ------ | ---- | ---- |
| `subjectId` | string | 예   |      |
| `file`      | File   | 예   | PDF  |

| 응답         | 타입   | null 가능 |
| ------------ | ------ | --------- |
| `materialId` | string | 아니요    |
| `jobId`      | string | 아니요    |

#### `GET /api/materials/jobs/:jobId` — 분석 진행

| 응답         | 타입     | null 가능 | 설명                          |
| ------------ | -------- | --------- | ----------------------------- |
| `jobId`      | string   | 아니요    |                               |
| `materialId` | string   | 아니요    |                               |
| `subjectId`  | string   | 아니요    |                               |
| `status`     | string   | 아니요    | `running` · `done` · `failed` |
| `ocr`        | bool     | 예        | 스캔본: 글자 인식으로 분석    |
| `steps`      | string[] | 아니요    | 단계 이름                     |
| `current`    | number   | 아니요    | 지금 단계 번호                |
| `failedStep` | number   | 예        | 실패한 단계                   |
| `progress`   | number   | 예        | 진행 %                        |
| `reused`     | bool     | 예        | 저장된 결과 재사용 (PDF-06)   |
| `reason`     | string   | 예        | 실패 이유                     |

#### `POST /api/materials/toc` — 목차 붙여넣기 (S3-2)

| 요청        | 타입   | 필수 |
| ----------- | ------ | ---- |
| `subjectId` | string | 예   |
| `text`      | string | 예   |

응답 `units[]`: `no` number · `title` string · `from` number · `to` number

#### `PATCH /api/materials/:id/units` — 시험 범위 체크 (S4, PDF-08)

| 요청     | 타입   | 필수 |
| -------- | ------ | ---- |
| `unitNo` | number | 예   |
| `inExam` | bool   | 예   |

응답: `subjectId` string · `unitNo` number · `inExam` bool

#### `GET /api/materials/:id/file` — 원본 PDF (QZ-13 · 집중 모드 자료 열기)

응답: PDF 파일 그대로 (`Content-Type: application/pdf`, `success` 틀 없음). 없으면 `404 NO_FILE`.

#### `GET /api/units?subjectId=` · `PUT /api/units` — 단원 (S4)

| 요청 (`PUT`)           | 타입     | 필수 | 설명                   |
| ---------------------- | -------- | ---- | ---------------------- |
| `subjectId`            | string   | 예   |                        |
| `units[].id`           | string   | 예   |                        |
| `units[].no` · `title` | –        | 예   |                        |
| `units[].concepts`     | string[] | 예   |                        |
| `units[].from` · `to`  | number   | 예   | 쪽 범위                |
| `units[].difficulty`   | string   | 선택 | `low` · `mid` · `high` |
| `units[].quizless`     | bool     | 선택 |                        |

| 응답                  | 타입     | null 가능 | 설명                              |
| --------------------- | -------- | --------- | --------------------------------- |
| `subjectId`           | string   | 아니요    |                                   |
| `file`                | object   | 예        | 자료 정보 (화면 표시용)           |
| `units[].id`          | string   | 아니요    |                                   |
| `units[].no`          | number   | 아니요    |                                   |
| `units[].title`       | string   | 아니요    |                                   |
| `units[].concepts`    | string[] | 예 ([])   |                                   |
| `units[].from` · `to` | number   | 아니요    |                                   |
| `units[].difficulty`  | string   | 예        | `low` · `mid` · `high` (AI 추정)  |
| `units[].quizless`    | bool     | 예        | 목차 직접 입력 범위               |
| `units[].inExam`      | bool     | 예 (true) | 시험 범위                         |
| `units[].evidence`    | array    | 예 ([])   | 나눈 근거 문장 (서버가 만드는 값) |
