# 服务器镜像：只跑编排/DB/LLM 与纯业务，浏览器采集由卖家本机 Agent 承担。
# Playwright 是 Python 依赖之一，但服务器在远程模式下不调用浏览器，无需 `playwright install`。
FROM python:3.12-slim

WORKDIR /app

# 默认保持官方 PyPI；网络受限服务器可通过 Compose build args 指定可信镜像。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=300

COPY pyproject.toml ./
COPY app ./app

# 从 pyproject 解析依赖（requirements-dev.lock.txt 里的 playwright 是 Windows wheel，不能用于 Linux）。
RUN pip install --no-cache-dir --retries 10 --timeout "${PIP_DEFAULT_TIMEOUT}" \
    --index-url "${PIP_INDEX_URL}" .

COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

RUN addgroup --system keyword \
    && adduser --system --ingroup keyword --home /app keyword \
    && mkdir -p /app/data \
    && chown -R keyword:keyword /app

ENV KEYWORD_STORAGE_ROOT=/app/data
USER keyword

EXPOSE 8000

# 单进程运行；多 worker 需要 Postgres checkpointer，见 docs/LangGraph编排.md。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
