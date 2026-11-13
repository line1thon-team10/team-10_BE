# API 명세 진입점

구현 기준 명세는 [api/README.md](api/README.md)에서 읽는다.

FE 초안 복사본을 구현 완료 명세로 사용하지 않는다. 새 명세는 FE의 71개 HTTP 작업과 원문/BE 기반 추가5개, 도메인별 필드, 상태 전이, 실패·재시도, ERD 매핑, FE 변경표 및 수용 기준을 포함한다.

상태는 **FE 전달용 계약 정리 완료 / 구현·통합 검증 전**이다. 실제 BE 전체 구현 완료나 원격 서비스 검증을 뜻하지 않는다. PR #2의 저장소 구현은 이 명세 검토 이후 다시 정리하며 현재 명세에 맞는 것으로 간주하지 않는다.

문서 범위 검증:

```sh
python3 scripts/check_api_spec.py --fe ../FE
```

이 검증은 경로/링크만 확인하며 요청·응답 스키마 또는 실행 결과 검증을 대체하지 않는다.

FE 담당자에게는 [변경 안내](api/frontend-handoff.md)와 [PR #3](https://github.com/line1thon-team10/team-10_BE/pull/3)를 전달한다.
