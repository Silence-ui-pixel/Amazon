"""pytest fixtures：docker 化 PostgreSQL 测试库 + 逐测试 TRUNCATE 隔离。

- 测试库默认由 ``KEYWORD_DATABASE_URL`` 派生，库名追加 ``_test``；
  可用 ``KEYWORD_TEST_DATABASE_URL`` 显式覆盖。
- 会话级建库、建 schema（业务表 + checkpointer 表）一次完成；
  函数级在每次测试前截断所有表，保持测试互不干扰。
- PostgreSQL 不可达时在会话级 skip，不静默回退 SQLite。
"""

import os
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.db import config
from app.db.models import metadata
from app.workflows.checkpointer import postgres_checkpointer

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _test_url() -> str:
    override = os.environ.get("KEYWORD_TEST_DATABASE_URL")
    if override:
        return override
    url = make_url(config.database_url())
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def _ensure_test_database(url: str) -> None:
    target = make_url(url)
    admin = target.set(database="postgres")
    engine = create_engine(
        admin.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target.database}
            ).fetchone()
            if row is None:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_url()
    try:
        _ensure_test_database(url)
    except Exception as exc:  # noqa: BLE001 - 无法连接时跳过整场测试
        pytest.skip(f"PostgreSQL 不可用，跳过测试：{exc}")
    return url


@pytest.fixture(scope="session")
def engine(database_url: str) -> Generator[Engine, None, None]:
    eng = create_engine(database_url, pool_pre_ping=True)
    metadata.create_all(bind=eng)
    # checkpointer 表由 PostgresSaver.setup() 创建，不进 Alembic 元数据。
    with postgres_checkpointer(config.psycopg_url(database_url)):
        pass
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def db_engine(engine: Engine) -> Engine:
    return engine


@pytest.fixture()
def db(database_url: str) -> str:
    """测试库连接串，作为 create_app 的第一个位置参数。"""
    return database_url


@pytest.fixture(autouse=True)
def _clean_database(engine: Engine) -> Generator[None, None, None]:
    with engine.begin() as conn:
        business = ", ".join(metadata.tables)
        conn.execute(text(f"TRUNCATE {business} RESTART IDENTITY CASCADE"))
        conn.execute(
            text(f"TRUNCATE {', '.join(CHECKPOINT_TABLES)} RESTART IDENTITY CASCADE")
        )
    yield
