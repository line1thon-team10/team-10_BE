# RPI 배포

배포 대상은 `https://2026-line1.leehyowon14.dev`이며, 아래 구성은 BE와 고정된 AI submodule을 함께 실행한다. FE 호스팅은 포함하지 않는다. 도메인 연결만으로 API 명세 1.0의 미구현 기능이 완성되는 것은 아니다.

## 구성

### 신규 AI/업로드 작업의 초기 운영 설정 (POL-02)

| 설정 | 초기값 | 의미 |
|---|---|---|
| `MAX_PDF_UPLOAD_BYTES` | 26214400 (25MiB) | PDF 파일 자체의 상한. pinned AI API 기본 상한과 일치 |
| `AI_REQUEST_BUDGET_SECONDS` | 20 | 동기 요청 전체 예산. 0 초과 25 이하 |
| `AI_PDF_JOB_BUDGET_SECONDS` | 300 | 접수 후 실행하는 PDF 작업 한 시도의 총 예산 |

이번 값은 요구사항의 수치나 RPI 실측 성능 보장이 아니라 실행을 위한 운영 기본값이다. M-01/AI-01/M-02에서 설정 검증과 경계 처리를 구현하고, AI-02에서 실제 지연을 측정한 뒤 조정한다. 기존 Nginx 50m는 전체 HTTP 본문 제한이며 파일25MiB 검사와 다르다. 프록시 오류도 JSON 오류 계약과 맞춘다. 제한 변경은 환경 설정과 이 표를 함께 갱신하고, 현재 서버에 적용되었다고 표시하려면 배포 SHA와 환경 확인 근거를 남긴다.

## 배포 위치

| 위치 | 역할 |
| --- | --- |
| GitHub-hosted `ubuntu-24.04-arm` | PostgreSQL 통합 테스트, AI import 검증, ARM64 이미지 빌드·GHCR push |
| RPI5 `100.78.84.58` | 기존 Cloudflare Tunnel·Nginx, 전용 Actions runner `line1-rpi5-deploy` |
| RPI4 `100.78.68.53:3047` | 빌드된 BE 이미지와 PostgreSQL 17을 Docker Compose로 실행 |

RPI4의 실행 중·중지된 컨테이너 포트 매핑을 확인해 3047을 선택했다. API는 Tailscale IP에만 바인딩하고 DB는 호스트 포트를 열지 않는다. 기존 서비스의 컨테이너·볼륨·runner는 재사용하지 않는다.

RPI5 runner 경로는 `/home/ubuntu/actions-runner-line1`, RPI4 배포 경로는 `/home/ubuntu/apps/2026-line1`이다. RPI5의 `/home/ubuntu/.ssh/line1-deploy`는 배포 전용 키이며, RPI4 authorized_keys에서 RPI5의 Tailscale IP로 접속을 제한한다. runner는 현재 workflow의 배포 작업만 맡으며 PR 이벤트로 실행하지 않는다.

RPI5 Ubuntu 26.04에서 runner 2.338.2의 Worker가 작업 초기화 중 exit 139로 두 번 종료됐다. 같은 서버의 기존 설치와 같은 공식 2.338.0으로 교체한 뒤 checkout 및 배포 명령 실행을 확인했다. 자동 업데이트 설정은 끄지 않았으므로 업그레이드 후 같은 증상이 재발하면 runner 진단 로그와 버전을 먼저 확인한다. 기존 바이너리는 서버의 프로젝트 디렉터리에 백업했다.

## 최초 설정

1. 비공개 `team-10_AI`에 읽기 전용 deploy key를 등록하고 BE 저장소 Actions secret `AI_SUBMODULE_SSH_KEY`에 개인키를 저장한다. 사용자 승인 후 등록하며 키 내용을 저장소·로그에 남기지 않는다. BE submodule gitlink의 SHA를 그대로 checkout한다.
2. RPI4 배포 디렉터리에 아래 두 파일을 권한 `600`으로 준비한다. `.gitignore`에 포함되므로 커밋하지 않는다.
   - `infrastructure.env`: `POSTGRES_PASSWORD` (생성한 16진수 비밀번호), `BIND_IP=100.78.68.53`, `API_PORT=3047`.
   - `runtime.env`: [예시](../deploy/runtime.env.example)를 참고해 CORS·데모 모드·AI 키를 설정한다. DB 비밀번호는 초기화 후 env만 바꿔서는 변경되지 않는다.
3. RPI5에 BE 전용 Actions runner를 설치하고 `line1-deploy` label을 부여한다. 기존 runner의 설정을 바꾸지 않는다.
4. [Nginx 설정](../deploy/nginx.conf)을 RPI5의 별도 site로 설치하고 `nginx -t` 성공 후 reload한다. 기존 Cloudflare Tunnel을 사용한다. API 실행을 확인한 뒤 site를 활성화한다.

AI 소스가 포함되므로 GHCR `2026-line1-be` 이미지는 **private**으로 유지한다. 신규 패키지의 기본 private 설정에 더해 workflow에서 visibility를 검사한다. 공개 이미지·빌드 artifact·GHA layer cache로 배포하지 않는다. 배포할 때는 job의 일회성 `GITHUB_TOKEN`을 stdin으로 전달하고 임시 Docker 인증 디렉터리를 종료 시 삭제한다.

RPI4 Compose 플러그인은 `/home/ubuntu/.docker/cli-plugins/docker-compose`에 설치되어 있다. 임시 `DOCKER_CONFIG`에도 이 플러그인 디렉터리를 symlink로 연결한다. 기존 Docker 인증 파일은 복사하지 않는다. 플러그인 설치 위치가 바뀌면 workflow의 경로도 함께 갱신한다.

## 실행 및 확인

[workflow](../.github/workflows/deploy-rpi.yml)는 `main`, 초기 배포용 `feat/rpi-deployment` push 또는 수동 실행으로 동작한다. 다른 stacked 브랜치와 PR은 배포하지 않는다. 배포 브랜치 변경은 workflow에서 명시적으로 관리한다.

1. GitHub-hosted에서 테스트 후 `linux/arm64` 이미지를 빌드한다.
2. 배포 job이 digest로 고정된 이미지를 RPI4에 전달한다. 서버에서는 빌드하지 않는다.
3. [apply.sh](../deploy/apply.sh)는 동시 실행 lock → pull → DB 준비 → API 중지 → DB 백업 → Alembic upgrade → API 시작·healthcheck 순서로 실행한다.
4. 성공한 digest만 `deployed-image.txt`에 기록한다. 마지막에 공개 `/api/health`를 확인한다.

```bash
curl --fail https://2026-line1.leehyowon14.dev/api/health
curl --fail 'https://2026-line1.leehyowon14.dev/api/health?ai=true'
```

Swagger는 `/docs`, FE의 `VITE_API_BASE_URL`은 `https://2026-line1.leehyowon14.dev`다. 실제 FE origin을 `runtime.env`의 `CORS_ORIGINS`에 추가해야 한다. 현재 기본값은 배포 도메인과 로컬 개발 origin이다.

`GROQ_API_KEY`, `UPSTAGE_API_KEY` 없이도 서버·DB·데모 기능을 실행할 수 있다. AI 모듈 import 성공과 실제 외부 AI 호출 성공은 다르며, 키 없는 상태에서는 외부 AI 기능을 검증 완료로 보지 않는다. 설정 변경 후에는 직전 성공 digest를 사용해 API를 재생성한다.

## 실패와 복구

백업은 RPI4 `backups/<UTC 시각>.sql`, 데이터는 Compose의 프로젝트 전용 `postgres`, `storage`, `ai_cache` 볼륨에 보관한다. 백업 자동 삭제는 하지 않으므로 용량을 주기적으로 확인한다. `docker compose down -v`는 데이터를 삭제하므로 운영 절차에 사용하지 않는다.

- pull 실패: 기존 API를 중지하기 전에 종료한다.
- 백업·migration 실패: 새 API를 시작하지 않는다. 백업 실패 시 생성된 SQL은 불완전할 수 있으므로 복구에 사용하기 전에 확인한다.
- healthcheck 실패: 새 digest를 성공으로 기록하지 않는다. API 로그·DB revision을 확인한다.
- 실패 시 자동 DB downgrade 또는 이미지 rollback은 하지 않는다. migration 성공 후 이전 이미지가 새 스키마와 호환되지 않을 수 있기 때문이다.

수동 점검 시 RPI4 배포 디렉터리에서 `BE_IMAGE=$(cat deployed-image.txt)`를 export한 뒤 `docker compose --env-file infrastructure.env -f compose.yml ps` 또는 `logs --tail 100 api`를 사용한다. 첫 배포 실패에는 성공 digest 파일이 없으므로 Actions가 사용한 digest를 지정한다. 로그를 공유할 때 사용자 데이터·키를 제거한다.

## 명세와의 관계

이번 변경은 배포 구성이다. FE/BE API 계약이나 ERD는 변경하지 않는다. Nginx의 50 MiB 요청 제한은 임시 운영 설정이며 제품의 PDF 허용 크기로 확정한 값이 아니다. 파일 업로드를 구현할 때 요구사항·최종 API 명세와 다시 맞추고 충돌 내역을 해당 PR에 기록한다.
