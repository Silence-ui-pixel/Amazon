"""数据库连接与文件存储根目录配置；沿用「不自动加载 .env」约定，显式读进程环境变量。"""

import os
from pathlib import Path

from sqlalchemy import URL
from sqlalchemy.engine import make_url

DEFAULT_DATABASE_URL = "postgresql+psycopg://keyword:keyword@localhost:5433/keyword_library"


def database_url() -> str:
    """返回 SQLAlchemy 连接串；未设置时指向 Compose 里的本地 PostgreSQL。"""
    explicit = os.environ.get("KEYWORD_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    host = os.environ.get("KEYWORD_DATABASE_HOST", "").strip()
    if not host:
        return DEFAULT_DATABASE_URL
    return URL.create(
        "postgresql+psycopg",
        username=os.environ.get("POSTGRES_USER", "keyword"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        host=host,
        port=int(os.environ.get("KEYWORD_DATABASE_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "keyword_library"),
    ).render_as_string(hide_password=False)


def alembic_url(url: str) -> str:
    """Alembic ConfigParser 使用百分号插值，写入连接串前必须双写。"""
    return url.replace("%", "%%")


def storage_root() -> Path:
    """上传/采集等文件产物的根目录；数据库从文件改为 URL 后独立成变量。"""
    default = Path(__file__).resolve().parents[2] / "data"
    return Path(os.environ.get("KEYWORD_STORAGE_ROOT", str(default)))


def psycopg_url(url: str) -> str:
    """LangGraph PostgresSaver 需要纯 postgresql:// 连接串，从 SQLAlchemy URL 派生。"""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
