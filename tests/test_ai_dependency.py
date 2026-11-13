"""Submodule 기본 경로와 개발용 경로, 초기화 누락을 검증해요."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_default_ai_path_is_pinned_submodule_even_from_another_cwd(monkeypatch, tmp_path, value):
    if value is None:
        monkeypatch.delenv("AI_ENGINE_PATH", raising=False)
    else:
        monkeypatch.setenv("AI_ENGINE_PATH", value)
    monkeypatch.chdir(tmp_path)
    with patch("dotenv.load_dotenv"):
        config = runpy.run_path(str(ROOT / "app/config.py"))
    assert config["AI_ENGINE_PATH"] == ROOT / "vendor/team-10_AI"


@pytest.mark.parametrize("relative", [True, False])
def test_development_override_is_independent_of_cwd(monkeypatch, tmp_path, relative):
    value = "../AI" if relative else str(tmp_path / "AI source")
    monkeypatch.setenv("AI_ENGINE_PATH", value)
    monkeypatch.chdir(tmp_path)
    with patch("dotenv.load_dotenv"):
        config = runpy.run_path(str(ROOT / "app/config.py"))
    expected = (ROOT / value).resolve() if relative else Path(value)
    assert config["AI_ENGINE_PATH"] == expected


def run_isolated(source, code):
    # app.main은 import하지 않아 DB/시드/외부 AI 요청을 시작하지 않아요.
    env = {**os.environ, "AI_ENGINE_PATH": str(source), "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=source.parent, env=env,
        capture_output=True, text=True, timeout=15, check=True,
    )


@pytest.mark.parametrize("exists", [False, True])
def test_missing_or_uninitialized_submodule_does_not_import_other_services(tmp_path, exists):
    source = tmp_path / "ai"
    if exists:
        source.mkdir()  # recursive clone을 하지 않은 경우 빈 gitlink 디렉터리
    run_isolated(source, """
from app.ai import engine
assert engine.status()['found'] is False
try:
    engine.module('learning_metrics')
except ModuleNotFoundError as exc:
    assert 'git submodule update --init --recursive' in str(exc)
else:
    raise AssertionError('Missing AI source should fail before module lookup')
""")


def test_selected_ai_source_is_loaded_lazily_and_cached(tmp_path):
    source = tmp_path / "ai"
    services = source / "services"
    services.mkdir(parents=True)
    (services / "learning_metrics.py").write_text("SOURCE = 'selected-ai-commit'\n")
    run_isolated(source, """
import sys
from pathlib import Path
from app.ai import engine
assert 'services.learning_metrics' not in sys.modules
first = engine.module('learning_metrics')
assert first.SOURCE == 'selected-ai-commit'
assert Path(first.__file__).parent.parent == engine.AI_ENGINE_PATH
assert engine.module('learning_metrics') is first
""")
