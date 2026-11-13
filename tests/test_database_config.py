from pathlib import Path
import runpy
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", [None, "", " \t "])
def test_empty_database_url_uses_documented_default(monkeypatch, tmp_path, value):
    if value is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", value)
    monkeypatch.chdir(tmp_path)
    with patch("dotenv.load_dotenv"):
        config = runpy.run_path(str(ROOT / "app/config.py"))
    assert config["DATABASE_URL"] == f"sqlite:///{ROOT / 'pirochim.db'}"


def test_explicit_database_url_is_preserved(monkeypatch):
    url = "postgresql+psycopg://user:percent%25word@localhost/study"
    monkeypatch.setenv("DATABASE_URL", url)
    with patch("dotenv.load_dotenv"):
        config = runpy.run_path(str(ROOT / "app/config.py"))
    assert config["DATABASE_URL"] == url
