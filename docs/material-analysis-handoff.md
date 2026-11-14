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
