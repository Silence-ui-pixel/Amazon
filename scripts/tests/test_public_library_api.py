from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.v1.public_library import build_public_library_router
from app.db.repository import Repository


@pytest.fixture()
def published_api(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'publication.sqlite'}")
    project_id = str(uuid4())
    token = "a" * 43
    grants = tmp_path / "grants.json"
    grants.write_text(
        json.dumps(
            [
                {
                    "sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "project_id": project_id,
                    "version": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    document = {
        "id": str(uuid4()),
        "project_id": project_id,
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "request": {"traffic_id": str(uuid4()), "traffic_version": 1},
        "publication_version": 1,
        "published_at": datetime.now(UTC).isoformat(),
        "source_sha256": "internal",
        "snapshots": {"raw": "internal evidence"},
        "entries": [
            {
                "keyword": keyword,
                "tier": "L1",
                "roots": {},
                "priority": "normal",
                "evidence": {
                    "phrase": {"keyword": keyword, "evidence": []},
                    "metrics": {"rank": 10},
                },
            }
            for keyword in ("desk", "desk mat")
        ],
        "suggestions": [],
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE keyword_library_publications (project_id TEXT, version INT, document TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO keyword_library_publications VALUES (:project_id, 1, :document)"
            ),
            {"project_id": project_id, "document": json.dumps(document)},
        )
    app = FastAPI()
    app.include_router(
        build_public_library_router(Repository(engine, tmp_path), grants)
    )
    with TestClient(app) as client:
        yield client, project_id, {"Authorization": f"Bearer {token}"}, grants
    engine.dispose()


def test_public_api_paginates_and_excludes_internal_fields(published_api):
    client, project_id, headers, _ = published_api
    path = f"/api/public/v1/libraries/{project_id}/versions/1"
    response = client.get(path, params={"limit": 1}, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["total"] == 2
    assert result["next_offset"] == 1
    assert result["items"] == [
        {
            "keyword": "desk",
            "aba_rank": 10,
            "tier": "L1",
            "priority": "normal",
            "usage": "pending",
        }
    ]
    assert "internal" not in response.text
    assert (
        client.get(path, params={"offset": 1, "limit": 1}, headers=headers).json()[
            "next_offset"
        ]
        is None
    )
    assert client.get(path, params={"limit": 201}, headers=headers).status_code == 422
    assert client.post(path, headers=headers).status_code == 405


def test_public_api_requires_key_and_enforces_release_scope(published_api):
    client, project_id, headers, grants = published_api
    base = "/api/public/v1/libraries"
    path = f"{base}/{project_id}/versions/1"
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert (
        client.get(path, headers={"Authorization": "Bearer " + "b" * 43}).status_code
        == 401
    )
    assert (
        client.get(f"{base}/{project_id}/versions/2", headers=headers).status_code
        == 404
    )
    assert (
        client.get(f"{base}/{uuid4()}/versions/1", headers=headers).status_code == 404
    )
    grants.write_text("[]", encoding="utf-8")
    assert client.get(path, headers=headers).status_code == 401
    grants.unlink()
    assert client.get(path, headers=headers).status_code == 503
