# 자료·단원 API

근거: PDF-01·07·08(p.16), 와이어 S3·S4. FE의 자료 목록 `files[]`는 이미 다중 PDF를 지원한다. **수정 대상은 단원 조회·저장의 자료 식별과 개념별 시험 범위**다. 기존 BE `schema/openapi-0.7.0.json`의 `UnitsReplace`, `ConceptInput` 의미를 채택하고 FE 경로/필드 이름을 유지한다.

## 자료 업로드·조회

| 요청 | 입력 | `data` 응답 |
|---|---|---|
| `GET /api/materials` | 없음 | `{subjects:[{id,name,color,files:[{id,name,pages,status,jobId,quizless,ocr}]}]}` |
| `POST /api/materials` | FormData `subjectId`, `file` 필수 | `{materialId,jobId}` |
| `GET /api/materials/jobs/:jobId` | 소유 작업 ID | `{jobId,materialId,subjectId,status,ocr,steps,current,failedStep,progress,reused,reason}` |
| `GET /api/materials/:id/file` | 자료 ID | PDF binary, 없으면 `404 NO_FILE` |
| `POST /api/materials/toc` | `{subjectId,materialId,text}` | `{units:[{no,title,from,to}]}`; 파싱만 하고 저장하지 않음 |

ID/이름/reason은 string, pages/current/failedStep/progress는 integer, steps는 string[], reused/ocr/quizless는 bool. 목록의 pages/jobId/ocr/quizless는 분석 전 null 가능. 작업의 ocr/reason/failedStep은 아직 판정되지 않았거나 해당 없으면 null. status는 running/done/failed. running의 progress=0..99, done=100, current는 0 기반 단계 인덱스. failedStep은 실패한 current, reason은 한국어 안내다.

- 과목당 여러 PDF를 보존한다. 새 업로드가 기존 자료를 교체하지 않는다. 동일 사용자·과목·파일 SHA256의 완료/진행 작업은 재사용, 실패 작업은 새 시도로 접수한다.
- PDF MIME과 실제 구문을 검증한다. 비PDF `415 NOT_PDF`, 잠금/손상으로 읽을 수 없으면 `400 BAD_REQUEST`. 운영 업로드 상한을 넘으면 `413 FILE_TOO_LARGE`; 50MiB를 제품 요구로 고정하지 않는다.
- OCR 성공이면 `ocr=true`. 저품질일 때만 `quizless=true`. OCR이라는 이유만으로 전부 퀴즈에서 제외하지 않는다. 분석 실패 시 수동 목차 입력을 안내한다. 품질 임계값은 p.22의 AI 확인 사항이며 FE는 서버 판정값으로 표시한다.
- 파일 저장과 작업 접수를 완료한 뒤 200을 반환한다. 오래된 분석 작업은 새 구조를 덮어쓰지 않는다. 스토리지 경로는 노출하지 않는다.

## 단원 조회·전체 저장

`GET /api/units?subjectId=…&materialId=…`: 두 ID 모두 필수다. `PUT /api/units`의 편집 요청은 아래 형식이다. 기존 단원/개념은 ID를 보내고 신규 항목만 ID를 생략한다.

```json
{
  "subjectId": "subject-id",
  "materialId": "material-id",
  "units": [{
    "id": "unit-id", "no": 1, "title": "자료구조", "from": 1, "to": 12,
    "difficulty": "low", "inExam": true,
    "concepts": [{"id": "concept-id", "title": "스택", "from": 1, "to": 5, "inExam": true}]
  }]
}
```

- `subjectId/materialId/units` 필수, units는 비어 있지 않은 배열. 각 단원의 no/title/from/to/concepts/inExam 필수, difficulty는 low/mid/high/null이며 생략 시 null. 개념의 title/from/to/inExam 필수. 배열 순서가 개념 순서다.
- no는 **자료 내** 유일한 양의 정수. `unitId`가 식별자이며 `unitNo`는 표시용이다. 서로 다른 PDF의 1단원을 합치지 않는다.
- 페이지는 실제 PDF 기준 정수 `1 <= from <= to <= pages`. 단원끼리 겹치지 않으며 개념은 소속 단원 안에 있어야 한다. 개념 간 중첩은 허용하고 페이지 집계는 합집합이다. `concepts:[]`는 수동 목차 등 개념을 추출하지 못한 경우 허용한다.
- 최종 시험 범위는 inExam=true 단원의 선택된 개념 페이지 합집합이다. 개념이 없는 단원은 단원 페이지 전체를 사용한다. 개념이 있지만 모두 제외하면 그 단원의 시험 범위는 0쪽이다. 보충/문항도 이 식별 관계를 사용한다.
- quizless/ocr/evidence는 서버 출력이며 편집 요청에서 받지 않는다. 수동 입력만으로 문항 근거가 생성됐다고 간주하지 않는다.
- 제목·순서·난이도·inExam만 변경하면 ID 유지. 페이지/개념 구성 변경은 영향받은 구조만 retire 후 신규 ID. 삭제된 단원의 과거 문항·학습 기록·점수를 보존하며 새 ID에 복제하지 않는다.

GET의 `data` 및 저장 완료의 `data.structure`:

```json
{
  "subjectId": "subject-id", "materialId": "material-id",
  "file": {"id": "material-id", "name": "강의.pdf", "pages": 50},
  "units": [{
    "id": "unit-id", "no": 1, "title": "자료구조", "from": 1, "to": 12,
    "difficulty": "low", "inExam": true, "quizless": false,
    "evidence": ["목차와 본문 제목에 근거"],
    "concepts": [{"id": "concept-id", "title": "스택", "from": 1, "to": 5, "inExam": true}]
  }]
}
```

존재하지 않는/타인 자료는 `404 NO_MATERIAL`, 빈 단원은 `400 NO_UNITS`, 소속 불일치·페이지 오류는 `422 INVALID_INPUT`. 자료가 없는 경우에는 먼저 자료 목록의 `files:[]`를 표시하며 불완전한 ID로 GET units를 호출하지 않는다.

## 개념별 시험 범위와 승인 후 편집

`PATCH /api/materials/:id/units` (`:id`는 materialId):

```json
{"unitId":"unit-id","inExam":true,"concepts":[{"conceptId":"concept-id","inExam":false}]}
```

unitId 필수, inExam 또는 비어 있지 않은 concepts 중 하나 이상 필요. 생략한 단원/개념 상태는 유지한다. concepts의 중복 ID, 다른 단원 소속 ID는 거절한다. 단원 체크 해제 시 개념의 선택 상태는 보존하되 유효 시험 범위에서는 제외한다.

PUT/PATCH 모두 승인 계획이 없으면 바로 저장하고 `{applied:true,previewId:null,structure:위 구조}`를 반환한다. 승인 계획이 있으면 **아무것도 변경하지 않고** `{applied:false,previewId,structure:후보 구조,changes:[{label,before,after}],days:[{date,text}],note}`를 반환한다. changes 값은 표시용 string, note는 string|null이다. FE는 비교 화면을 표시한다.

승인은 동일 PUT/PATCH 경로에 `{previewId}`만 전달한다. 성공 `{applied:true,previewId,structure:확정 구조}`. [후보 공통 규칙](decisions.md)을 적용해 해당 과목의 남은 범위만 재배분하고 기존 학습·퀴즈·책·보충은 보존한다. 구조 교체로 ID가 바뀌면 응답 구조를 다시 저장한다. 모든 시험 범위를 제외하거나 가용시간에 배정 불가하면 `409 PLAN_INFEASIBLE`, 승인 직전 다른 편집이 적용됐으면 `409 PREVIEW_STALE`이다.
