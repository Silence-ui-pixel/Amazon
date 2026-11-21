"""容器数据库配置必须安全处理随机密码中的 URL 特殊字符。"""

import pytest
from sqlalchemy.engine import make_url

from app.db import config


def test_database_url_encodes_component_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYWORD_DATABASE_URL", "")
    monkeypatch.setenv("KEYWORD_DATABASE_HOST", "postgres")
    monkeypatch.setenv("KEYWORD_DATABASE_PORT", "5432")
    monkeypatch.setenv("POSTGRES_USER", "keyword_prod")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss%word:/?#[]")
    monkeypatch.setenv("POSTGRES_DB", "keyword_library")

    url = config.database_url()
    parsed = make_url(url)
    assert parsed.password == "p@ss%word:/?#[]"
    assert parsed.host == "postgres"
    assert parsed.database == "keyword_library"


def test_alembic_url_escapes_configparser_percent() -> None:
    assert config.alembic_url("postgresql://user:p%40ss@postgres/db") == (
        "postgresql://user:p%%40ss@postgres/db"
    )
