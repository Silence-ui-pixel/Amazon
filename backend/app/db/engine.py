"""SQLAlchemy 同步引擎装配；业务库、工作流投影与 checkpointer 共用同一数据库。"""

from sqlalchemy import Engine, create_engine


def build_engine(url: str) -> Engine:
    """pool_pre_ping 应对容器重启；同步引擎配合现有全同步数据层。"""
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
    )
