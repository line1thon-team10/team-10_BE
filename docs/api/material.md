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
- PDF MIME과 실제 구문을 검증한다. 비PDF `415 NOT_PDF`, 잠금/손상으로 읽을 수 없으면 `400 BAD_REQUEST`. 운영 업로드 상한을 넘으면 `413 FILE_TOO_LARGE`. POL-02 초기 설정은 `MAX_PDF_UPLOAD_BYTES=26214400`(25MiB, pinned AI 업로드 기본 상한과 일치)이며 제품 요구가 아니다. 정확히 상한인 파일은 허용하고 한 바이트 초과부터 거절한다. Content-Length를 신뢰하지 않고 읽는 동안 누적 검사하며 거절된 임시 파일/새 작업을 남기지 않는다.
- OCR 성공이면 `ocr=true`. 저품질일 때만 `quizless=true`. OCR이라는 이유만으로 전부 퀴즈에서 제외하지 않는다. 분석 실패 시 수동 목차 입력을 안내한다. 품질 판정은 아래 POL-03의 pinned AI 휴리스틱을 사용하며 OCR 신뢰도 실측값으로 해석하지 않는다. FE는 서버 판정값으로 표시한다.
- 파일 저장과 작업 접수를 완료한 뒤 200을 반환한다. 오래된 분석 작업은 새 구조를 덮어쓰지 않는다. 스토리지 경로는 노출하지 않는다.

## 품질 출력과 quizless 정책 (POL-03)

원문 p.22는 품질 판정을 AI 확인 사항으로 남겼다. AI pin `6d74ffb6c455a306e912252013d76f93e30a6210`의 `services/pdf_quality.py`에는 이미 `evaluate_pdf_quality`가 있지만 `pdf_analyzer.py`는 이를 호출하거나 보고서를 반환하지 않는다. 따라서 성공 분석을 곧바로 출제 가능으로 취급하던 경로는 M-02에서 보완한다. 새로운 confidence 임계값을 만들지 않고 기존 알고리즘을 `text-quality-v1`로 버전 고정한다.

- 페이지별 의미 문자(한글/영문/숫자) 40개 이상, 대체 문자 비율 0.05 미만이면 읽을 수 있는 페이지다. 전체 실제 페이지 중 읽을 수 있는 페이지 비율이 0.35 이상이고 한 쪽 이상이면 `status=pass, quiz_allowed=true`; 나머지는 `no_quiz,false`다. 비교는 반올림 전 값으로 하며 누락 페이지도 전체 분모에 포함한다. 이는 추출 텍스트 휴리스틱이며 OCR 인식 정확도/강의 내용 정확성을 보장하지 않는다.
- BE는 자료 SHA/추출 경로/분석 버전과 함께 보고서의 method, status, reason, 전체/읽을 수 있는 페이지 수 및 페이지별 신호를 저장한다. 내부 보고서가 외부 DTO의 `quizless = not quiz_allowed`를 결정한다. 문항은 추가로 실제 근거가 있고 허용 범위 안에 있는지 검증해야 한다.
- 네이티브 텍스트를 정상 추출한 경로는 `ocr=false`. 실제 OCR 경로를 실행하고 정상 결과를 받은 경우만 `ocr=true`; 업로드 MIME이나 스캔 추정만으로 true를 쓰지 않는다. 읽을 수 없는 네이티브 텍스트는 OCR을 시도할 수 있지만 그 중간 판정으로 최종 quizless를 확정하지 않는다.
- 정상 OCR + 품질 pass는 출제 가능, 정상 OCR + no_quiz는 완료된 저품질 자료로 저장하고 출제 제외, 공급자 실패/키 누락/시간 초과/잘못된 구조는 failed 작업이다. 분석 실패를 성공한 OCR 또는 0점 품질 보고서로 꾸미지 않는다. 수동 목차만 있는 범위도 근거 문항이 생기기 전에는 quizless다.
- 2026-11-14 로컬 확인: AI/DB/OS 각 30/30쪽, realtime 8/8쪽의 네이티브 텍스트가 pass; scanned 0/6쪽은 no_quiz였다. 합성된 정상/깨진/빈 추출 텍스트도 pass/no_quiz/no_quiz로 구분했다. **실제 OCR 호출 결과는 키가 없어 검증하지 않았다.** AI-02에서 스캔 샘플 실호출, 페이지 매핑과 품질 적합성을 따로 확인한다. OCR 여부만 보고 전부 제외하는 대체 규칙은 허용하지 않는다.

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

PUT/PATCH 모두 해당 과목이 draft이거나 승인 계획이 없으면 바로 저장하고 `{applied:true,previewId:null,structure:위 구조}`를 반환한다. 해당 과목이 active이고 승인 계획이 있으면 **아무것도 변경하지 않고** `{applied:false,previewId,structure:후보 구조,changes:[{label,before,after}],days:[{date,text}],note}`를 반환한다. changes 값은 표시용 string, note는 string|null이다. FE는 비교 화면을 표시한다.

승인은 동일 PUT/PATCH 경로에 `{previewId}`만 전달한다. 성공 `{applied:true,previewId,structure:확정 구조}`. [후보 공통 규칙](decisions.md)을 적용해 해당 과목의 남은 범위만 재배분하고 기존 학습·퀴즈·책·보충은 보존한다. 구조 교체로 ID가 바뀌면 응답 구조를 다시 저장한다. 모든 시험 범위를 제외하거나 가용시간에 배정 불가하면 `409 PLAN_INFEASIBLE`, 승인 직전 다른 편집이 적용됐으면 `409 PREVIEW_STALE`이다.

## 구조 편집의 적용 경계

기존 사용자가 새 draft 과목을 추가하는 동안에는 기존 승인 계획이 있어도 draft 목차를 바로 편집한다. 이 저장은 기존 계획 revision이나 태스크를 변경하지 않는다. 목표와 일정 확인 및 초기 계획 승인 전부터 자료를 편집해야 하므로 전역 plan 존재만으로 모든 과목을 preview 경로에 넣지 않는다.

활성 과목의 구조 preview는 현재 자료, 목표, 일정, 기록, 집중 세션, 계획 revision을 기준으로 계산한다. 계산 중에는 구조나 태스크를 저장하지 않으며, 같은 기준과 입력의 후보를 재사용한다. 승인 시 자료 구조와 남은 학습 태스크를 한 트랜잭션으로 반영하고, 같은 previewId는 최초 성공 응답을 재생한다. 날짜나 입력 변경, 다른 후보 승인 뒤에는 PREVIEW_STALE이다.

페이지 범위 또는 개념 구성(추가/삭제/범위 변경)이 달라진 단원만 retire하고 새 단원과 개념 ID를 부여한다. 다른 단원은 유지한다. 제목, 순서, 난이도, 시험 선택만 변경하면 기존 ID 및 mastery를 유지한다. retire한 ID의 기록, 퀴즈, 책, 보충은 삭제하거나 신규 ID로 복제하지 않는다. 새 수동 구조의 evidence는 빈 배열이며 quizless=true다. 실제 학습한 페이지는 자료별 합집합으로 새 구조에서도 차감한다.

탐색 자원 부족은 불가능 판정과 구분해 503 PLAN_SEARCH_LIMIT으로 반환한다. 과거의 미완료 보충이나 복습이 남은 경우에는 공통 계획 입력의 409 OVERDUE_OBLIGATIONS 경계를 사용하며, L-01의 날짜 정산과 이월 후 재요청한다. 해당 의무를 버리고 성공 후보를 만들지 않는다.
