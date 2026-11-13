# AI submodule과 stacked 개발

BE는 `vendor/team-10_AI`에서 [AI 저장소](https://github.com/line1thon-team10/team-10_AI)의 코드를 직접 import한다. 별도 AI HTTP 서버는 필요하지 않다. 최초 연결은 AI `main`의 `6d74ffb6c455a306e912252013d76f93e30a6210`이다. 실제 사용 버전은 이 문서가 아니라 **BE 커밋의 gitlink**가 결정한다.

`.gitmodules`의 `branch=main`은 명시적인 원격 업데이트 시 참고하는 브랜치다. 일반 clone/update는 BE에 고정된 커밋을 checkout하며, AI main이 바뀌었다고 BE 코드가 자동으로 바뀌지 않는다. AI 파일을 BE 커밋에 복사하거나 두 저장소의 Git 이력을 merge하지 않는다.

## 설치와 실행

```bash
git clone --recurse-submodules https://github.com/line1thon-team10/team-10_BE.git
cd team-10_BE
# 기존 checkout 또는 BE 브랜치 전환 뒤에도 실행
git submodule sync --recursive
git submodule update --init --recursive
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-ai.txt
```

Windows에서는 `.venv/Scripts/python`을 사용한다. AI 저장소가 비공개이므로 개발자/빌드 작업의 Git 자격 증명에 AI 읽기 권한이 있어야 한다. 토큰을 `.gitmodules` URL이나 커밋에 넣지 않는다.

- `AI_ENGINE_PATH` 미설정/빈 값/공백은 `BE/vendor/team-10_AI`를 사용한다. 실행 디렉터리와 무관하다.
- AI를 별도 checkout에서 개발할 때만 `.env`에 `AI_ENGINE_PATH=../AI`처럼 덮어쓸 수 있다. 상대 경로는 BE 루트 기준이다. 배포에서는 기본값을 사용해 gitlink와 실제 소스를 일치시킨다.
- 기존 `.env`에 외부 AI 경로가 들어 있으면 그 값이 우선한다. submodule을 사용하려면 비우거나 제거한다.
- submodule을 초기화하지 않았으면 `/api/health?ai=true`의 `ai.found=false`, AI 호출은 초기화 명령을 포함한 모듈 로딩 오류를 반환한다. 빈 디렉터리를 정상 설치로 표시하지 않는다.
- Python 패키지는 BE의 `requirements.txt`와 `requirements-ai.txt`를 함께 사용한다. AI 원본 requirements의 PyMuPDF 충돌을 보정한 기존 BE 목록을 유지했다. AI 업데이트 PR에서 이 목록의 호환성도 재확인한다. gitlink 고정이 Python 전이 의존성 전체의 lock을 뜻하지는 않는다.
- 현재 데모는 submodule의 `data/demo` PDF와 `data/ground_truth` 목차를 읽는다. 구 DS/OS/NW `seed_quiz.json`은 현재 시드에 쓰지 않는다. 원본 AI 파일을 BE에 복사하거나 ignore를 무시해 추가하지 않는다. 새 문항은 BE `app/seed/package_questions.py`의 로컬 데모 문항이다.

## 저장소별 변경 순서

1. AI 기능 변경은 AI 저장소의 기능 브랜치에서 커밋하고 AI PR을 만든다. BE는 AI의 함수 입출력을 `app/ai`와 서비스에서 FE 계약으로 변환한다.
2. BE 기능 브랜치에서 필요한 AI 커밋을 checkout한다. 먼저 해당 AI 커밋을 원격에 push해 다른 개발자와 빌드가 가져올 수 있게 한다.
3. BE에서는 submodule 포인터 변경과 BE adapter 변경을 각각 검토 가능한 커밋으로 만든다. AI 소스 변경을 BE 커밋인 것처럼 취급하지 않는다.
4. BE PR에 AI PR/커밋, 이전→신규 SHA, 변경된 함수 계약, 호환성 검증을 적는다. AI PR이 미병합이면 BE PR에도 의존성을 표시한다.
5. AI PR을 먼저 머지하고, BE가 가리키는 커밋이 AI main에서 도달 가능한지 확인한다. AI가 squash/rebase 머지되면 BE 포인터를 실제 병합 커밋으로 갱신하고 다시 검증한다. 그 후 BE 스택을 아래부터 머지한다.

검토한 커밋으로 갱신하는 명령은 다음과 같다. `<검토한-AI-SHA>`는 실제 커밋으로 바꾼다.

```bash
git -C vendor/team-10_AI fetch origin
git -C vendor/team-10_AI checkout --detach <검토한-AI-SHA>
git diff --submodule=log
git add vendor/team-10_AI
git commit -m "build(ai): 검증한 AI 버전으로 갱신"
```

submodule 안에 미커밋 변경이 있으면 먼저 AI 작업으로 처리한다. BE에 gitlink만 커밋해도 AI 안의 미커밋 파일은 전달되지 않는다. 일반 빌드/배포에서 `git submodule update --remote`를 실행하지 않는다.

## 빌드와 검증

현재 `.github/workflows/deploy-rpi.yml`은 BE gitlink의 AI 커밋을 읽어 비공개 저장소를 체크아웃한다. Dockerfile은 services와 데모 PDF·목차 폴더를 함께 포함한다. 자세한 빌드·배포는 [RPI 배포 문서](rpi-deployment.md)를 따른다. 새 시드 코드만 배포하고 AI 데이터 폴더를 제외하면 시드 적재가 실패한다.

```bash
git submodule status --recursive
.venv/bin/python -m pytest -q tests
```

`git submodule status`의 `-`는 미초기화, `+`는 BE에 고정된 커밋과 다른 checkout이다. 둘 다 배포 전에 해결한다. 네트워크 AI 호출 없이도 모듈 import와 경로 검증이 가능하다. 실제 PDF 분석·출제·계획·채점 품질은 해당 기능 PR에서 별도 검증한다.

현재 BE 스택은 `docs/api-specification` → `feat/ai-submodule`이다. AI 연결 PR은 명세 PR #3 위에 쌓으며, 후속 구현은 이 연결이 포함된 브랜치에서 진행한다.
