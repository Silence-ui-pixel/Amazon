"""Standalone ASGI entry point for integrations reading published keyword releases."""

import os
from pathlib import Path

from fastapi import FastAPI

from app.api.v1.public_library import build_public_library_router
from app.db.engine import build_engine
from app.db.repository import Repository


def create_public_api() -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    engine = build_engine(os.environ["KEYWORD_PUBLIC_LIBRARY_DATABASE_URL"])
    repository = Repository(engine, Path("/app"))
    keys_file = Path(os.environ["KEYWORD_PUBLIC_LIBRARY_KEYS_FILE"])
    app.include_router(build_public_library_router(repository, keys_file))
    return app
