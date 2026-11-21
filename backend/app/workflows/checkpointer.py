"""PostgreSQL checkpointer 装配；LangGraph 版本差异只在这一处处理。"""

from collections.abc import Generator
from contextlib import contextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver

Checkpointer = BaseCheckpointSaver[str]


@contextmanager
def postgres_checkpointer(url: str) -> Generator[PostgresSaver, None, None]:
    """使用独立连接池的 PostgresSaver；checkpoint 表由 setup() 创建。"""
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
        yield saver
