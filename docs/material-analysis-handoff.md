# 자료 분석 실행 경로

M-02는 `python -m app.workers.materials --loop`로 실행한다. 업로드 응답 전에 원본과 queued 작업은 저장되며 GET은 작업을 실행하지 않는다. worker를 별도 프로세스로 배치해야 실제 분석이 진행된다. 앱 lifespan 및 운영 Compose는 이 변경에서 수정하지 않았다.

`ai_jobs.result`는 `material-job-v1` envelope다. lease token, 만료, 현재 단계, OCR 판정 및 완료 분석을 보관한다. 이 브랜치의 기존 writer를 검색한 결과 자료 업로드는 result를 쓰지 않고 gateway는 `ai_saved_results`/`ai_usage_logs`만 쓴다. 재접수 실패 시 새 job을 만들고, 중단된 running은 전체 PDF 예산+10초 뒤 재선점한다. retry_count와 token, 사용자 잠금, 만료를 함께 확인하므로 늦은 worker는 반영할 수 없다. 실패의 원문 예외는 공개하지 않는다.

분석은 DB 트랜잭션 밖에서 gateway의 전체 300초 기본 예산으로 수행한다. 네이티브 추출 품질이 낮으면 강제 OCR을 실행하고, 정상 OCR 출력의 품질을 다시 판정한다. [Upstage 공식 문서](https://console.upstage.ai/api/parse/document-parsing)의 `ocr=force` 계약을 사용한다. 실제 OCR 성공 응답만 method=ocr다. 공급자 키 누락·실패·timeout은 failed이며 빈 성공 품질로 치환하지 않는다. 업로드 외관만으로 OCR 여부를 정하지 않는다. pinned `evaluate_pdf_quality`의 결과를 `text-quality-v1`로 재검산한다.

Groq와 OCR adapter는 전송 재시도 및 redirect를 끄고 매 전송에 남은 예산을 전달한다. 호출량은 gateway transport 관측을 사용하며 공유 디스크 캐시는 없다. 기존 pinned GroqService의 6회 재시도 및 PDF parser의 고정 180초 timeout을 호출하지 않는다. 저장 결과는 사용자와 SHA/analysisVersion별로 격리한다.

저장 직전 자료 소유권·SHA·분석 버전·구조 hash·최신 job을 다시 검증한다. 변경된 자료는 MATERIAL_STALE로 끝나고 현재 구조를 건드리지 않는다. 재접수 당시 이미 단원 이력이 있으면 분석 결과만 저장하고 단원/개념은 보존한다. 신규 빈 자료에만 단원·개념을 생성한다. 원본 페이지 범위·단원 비중첩·개념 소속을 검증하며 사용자 편집을 분석 결과로 덮지 않는다.

로컬 검사: `tests/test_material_adapter.py`, `tests/test_material_analysis.py`. 실제 유료 AI/OCR 호출 품질은 별도 AI-02 범위다. 이 문서는 배포 완료를 뜻하지 않는다.

## 수동 목차 해석

M-03 `POST /api/materials/toc`는 자료 소유권과 과목 관계·실제 페이지 수를 먼저 확인한다. `1. 제목 1~5쪽` 또는 `1) 제목 p1-5`처럼 명확한 한 줄 형식은 결정적으로 파싱하며, 나머지 자연어는 사용자·자료 버전에 격리된 gateway `nl_parse`를 사용한다. 두 경로 모두 양의 유일 단원 번호, 비어 있지 않은 제목, 물리 페이지 경계, 단원 비중첩을 확인한다. 명확한 입력의 잘못된 범위는 422, 외부 해석 실패는 503이며 임의로 내용을 고치거나 단원을 만들지 않는다. 응답 후보를 반환할 뿐 단원·개념·자료·분석 작업은 변경하지 않는다. 저장은 별도 `PUT /api/units`의 책임이다.

## Worker 실행 구성

Compose의 `material-worker`는 API와 같은 immutable image/DB/자료 volume을 사용하고 외부 포트를 열지 않는다.
기본 300초 PDF 예산에 맞춰 종료 유예는 320초다. 배포 스크립트는 기존 writer를 모두 중지한 뒤
DB를 백업하고 migration 후 worker를 시작한다. PID·유효시각 기반 heartbeat로 healthcheck하며,
SIGTERM/SIGINT는 현재 작업의 제한된 gateway 실행이 끝난 뒤 새 작업을 가져오지 않고 종료한다.
시간 예산을 늘리는 운영 설정은 종료 유예도 함께 조정해야 한다. 이전 앱에 worker 모듈이 없는
경우는 API만 시작할 수 있으나, 존재하는 worker의 import 실패나 Compose 누락은 배포 전 오류다.
현재 신규 API의 운영 배포 동결은 유지한다.
# 문항 준비 worker

자료 분석 worker는 대기 중인 PDF 분석을 처리한 뒤, 분석이 성공한 draft/active 과목의 근거 있는 단원에서 `question_pregenerate` 작업을 발견하고 실행한다. 문항 준비 실패는 PDF 분석 성공을 취소하지 않는다. 생성 상태와 오류는 `ai_jobs.kind='question_pregenerate'` 행에 남고, 실제 검증·저장된 문항만 퀴즈 카드와 출제에 포함된다.

현재 준비 묶음은 pinned AI bank의 6객관식+3단답+1서술 구성을 재사용하여 단원/난이도마다 10개를 요청한다. 일일 5개와 더 풀기 5개를 준비하기 위한 bounded 운영 묶음이며 제품 문항 상한이나 AI 품질 보장으로 정의하지 않는다. low를 모든 단원에 먼저 준비하고 mid/high를 뒤이어 준비한다. 각 작업은 일반 AI 요청 예산(기본20초, 최대25초)을 따르며 GET 부족 응답은 추가 AI 호출을 시작하지 않는다. 외부 키나 유효한 응답이 없으면 실패로 남는다.

실패 작업은 자동 polling으로 재호출하지 않는다. 운영자는 원인 복구 뒤 다음 조회에서 소유자와 실패 작업을 확인하고 명시적으로 재접수할 수 있다. 재접수는 외부 호출 없이 queued 상태만 커밋하고 기존 worker loop가 처리한다.

```sql
SELECT id, user_id, target_id, status, error_code
FROM ai_jobs WHERE kind = 'question_pregenerate' AND status = 'failed';
```

```sh
python -m app.workers.materials --retry-bank-job <job-uuid> --user-id <owner-uuid>
python -m app.workers.materials --loop
```

소유자가 다르거나 원문/단원 snapshot이 바뀐 작업은 재접수하지 않는다. 새 분석이나 구조 변경은 새 snapshot 작업으로 준비한다. 같은 snapshot은 고유 key와 lease로 중복 저장을 막고, 채택할 때 최신 소유·원문·구조를 다시 확인한다. 새 bank 질문은 생성 job ID를 보존하여 원문 분석 버전이 달라진 뒤 기존 bank에서 새 문제를 발급하지 않는다. 이미 저장된 풀이·완료 결과는 덮어쓰지 않는다.
