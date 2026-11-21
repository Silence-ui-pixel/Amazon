"""checkpointer：初始化 LangGraph 所需表，并可重复打开。"""

from sqlalchemy import Engine, inspect

from app.db import config
from app.workflows.checkpointer import postgres_checkpointer


def test_checkpointer_creates_tables_and_reopens(db: str, engine: Engine) -> None:
    url = config.psycopg_url(db)
    with postgres_checkpointer(url):
        pass
    tables = set(inspect(engine).get_table_names())
    assert {"checkpoints", "checkpoint_writes"} <= tables
    with postgres_checkpointer(url):
        pass
