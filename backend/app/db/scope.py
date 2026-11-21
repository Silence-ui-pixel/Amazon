"""共享的连接事务门面：业务库与工作流投影表共用同一套提交/回滚语义。

SQL 字符串沿用 ``?`` 占位符，由 ``ConnectionScope.execute`` 统一转成 SQLAlchemy
命名参数（``:p0,:p1,...``）；``text()`` 已正确识别 PostgreSQL 的 ``::`` 转型语法。
执行结果收敛成 tuple 行，保持与原 sqlite3 cursor 一致的取值方式。
"""

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import CursorResult


def qmark_to_named(sql: str) -> str:
    """把 ``?`` 占位符按序替换为 ``:p0,:p1,...``，跳过单引号字符串字面量。"""
    out: list[str] = []
    i = 0
    n = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            i += 1
            while i < length:
                out.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        out.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == "?":
            out.append(f":p{n}")
            n += 1
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class ResultAdapter:
    """把 SQLAlchemy CursorResult 收敛成 sqlite3 cursor 的最小接口（tuple 行）。"""

    def __init__(self, result: CursorResult[Any]) -> None:
        self._result = result

    def fetchone(self) -> tuple[Any, ...] | None:
        row = self._result.fetchone()
        return tuple(row) if row is not None else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in self._result.fetchall()]

    def keys(self) -> list[str]:
        return list(self._result.keys())


class ConnectionScope:
    """打开连接并开启事务；成功提交、异常回滚，退出关闭连接。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connection: Connection | None = None

    def __enter__(self) -> "ConnectionScope":
        self._connection = self._engine.connect()
        self._connection.begin()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._connection is not None
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        self._connection.close()

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> ResultAdapter:
        assert self._connection is not None
        named = qmark_to_named(sql)
        if params is None:
            bind: dict[str, Any] = {}
        elif isinstance(params, Mapping):
            bind = dict(params)
        else:
            bind = {f"p{i}": value for i, value in enumerate(params)}
        return ResultAdapter(self._connection.execute(text(named), bind))
