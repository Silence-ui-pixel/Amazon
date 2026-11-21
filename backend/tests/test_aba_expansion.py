"""第四步接口验收：保留正反来源，不把截断或失败当作穷尽。"""

import csv
import io
import json
import time
from copy import deepcopy
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_competitors import ready

from app.adapters.aba import AbaClient
from app.adapters.third_party import _xlsx
from app.db.repository import Repository
from app.domain.aba_expansion import ExpansionReport
from app.main import create_app


def prepare(db: str, engine: Engine, tmp_path: Path) -> str:
    client, base = ready(db, engine, tmp_path)
    discovered = client.post(
        base + "/competitors/discover",
        json={
            "source": "manual",
            "asins": ["B012345678", "B087654321"],
            "expected_version": 0,
        },
    ).json()
    saved = client.put(
        base + "/competitors/selection",
        json={
            "expected_version": discovered["version"],
            "reviews": [
                {
                    "asin": x["asin"],
                    "decision": "include",
                    "reason": "人工核对",
                    "price_override_reason": "核对价格",
                }
                for x in discovered["items"]
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    client.__exit__(None, None, None)
    return base


def wait(api: TestClient, url: str, report: str) -> dict[str, object]:
    for _ in range(200):
        result = api.get(url + "/" + report).json()
        if result.get("status") != "running":
            return result
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def replace_core_keywords(engine: Engine, tmp_path: Path, keywords: list[str]) -> None:
    with Repository(engine, tmp_path).connect() as conn:
        row = conn.execute(
            "SELECT id,document FROM core_selections ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        document = json.loads(row[1])
        template = document["items"][0]
        document["items"] = []
        for index, keyword in enumerate(keywords, start=1):
            item = deepcopy(template)
            item["id"] = str(uuid4())
            item["keyword"] = keyword
            item["search_frequency_rank"] = index
            document["items"].append(item)
        conn.execute(
            "UPDATE core_selections SET document=? WHERE id=?",
            (json.dumps(document), row[0]),
        )


@pytest.mark.parametrize("direction", ["forward", "reverse"])
@pytest.mark.parametrize("week_end", [None, "2026-10-17"])
def test_product_term_mode_cannot_enter_expansion(
    db: str, engine: Engine, tmp_path: Path, direction: str, week_end: str | None
) -> None:
    # 商品词模式的可选日期不能绕过第四步的显式报告周契约。
    base = prepare(db, engine, tmp_path)
    provider = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"reportsV2": [{"totalItems": 0, "rows": []}]})
        ),
    )
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=provider)) as api:
        response = api.post(
            base + "/aba-expansion",
            json={"direction": direction, "query_mode": "product_term", "week_end": week_end},
        )
        assert response.status_code == 422, response.text
        assert api.get(base + "/aba-expansion").json()["reports"] == []


@pytest.mark.parametrize("query_mode", ["root", "asin"])
def test_legacy_expansion_requires_an_explicit_week(db: str, engine: Engine, tmp_path: Path, query_mode: str) -> None:
    # 保证第一步新增可选日期后，旧正向/反向调用仍不能省略日期。
    base = prepare(db, engine, tmp_path)
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        response = api.post(
            base + "/aba-expansion", json={"direction": "forward", "query_mode": query_mode}
        )
        assert response.status_code == 422, response.text
        assert api.get(base + "/aba-expansion").json()["reports"] == []


def test_forward_reverse_evidence_export_cache_and_restore(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    calls: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query = next(
            x for x in body["filterSelections"] if x["id"] in ("asins", "search-term-freeform")
        )
        calls.append((query["id"], query["value"]))
        term = " Cooling   Blanket " if query["id"] == "asins" else "cooling blanket"
        first = row(term, 30)
        first.update(
            {
                "st-top-asin-click-share-1": "20%",
                "st-top-asin-conversion-share-1": "13%",
                "st-top-asin-2": "B000000002",
                "st-top-asin-conversion-share-2": "10%",
                "st-top-asin-3": "B000000003",
                "st-top-asin-conversion-share-3": "5%",
            }
        )
        return httpx.Response(
            200, json={"reportsV2": [{"totalItems": 2, "rows": [first, row("=1+1", 90)]}]}
        )

    client = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    url = base + "/aba-expansion"
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        assert api.get(url).json()["complete"] is False
        forward = api.post(
            url, json={"direction": "forward", "week_end": "2026-10-17", "max_rows": 100}
        ).json()
        forward = wait(api, url, str(forward["id"]))
        assert forward["status"] == "succeeded", forward
        assert forward["week_start"] == "2026-10-11"
        assert forward["week_end"] == "2026-10-17"
        assert calls == [("search-term-freeform", "cooling blanket")]
        reverse = api.post(
            url, json={"direction": "reverse", "week_end": "2026-10-17", "max_rows": 100}
        ).json()
        reverse = wait(api, url, str(reverse["id"]))
        assert reverse["status"] == "succeeded", reverse
        assert reverse["week_end"] == "2026-10-17"
        assert calls[1:] == [("asins", "B012345678,B087654321")]
        reverse_xlsx = api.get(url + f"/{reverse['id']}/queries/0/export.xlsx")
        assert reverse_xlsx.status_code == 200, reverse_xlsx.text
        assert (
            "filename*=UTF-8''%E7%AC%AC%E5%9B%9B%E6%AD%A5ABA%E5%8F%8D%E5%90%912%E4%B8%AAASIN.xlsx"
            in reverse_xlsx.headers["content-disposition"]
        )
        reverse_rows = [values for _, values in _xlsx(reverse_xlsx.content)]
        assert reverse_rows[0][2] == "查询竞对ASIN=B012345678,B087654321"
        assert reverse_rows[2][:4] == ["30", "Cooling   Blanket", "65.00%", "28.00%"]
        workspace = api.get(url + "?week_end=2026-10-17").json()
        assert workspace["complete"] is True
        assert workspace["comparison"] == {"both": 2, "forward_only": 0, "reverse_only": 0}
        page = api.get(url + f"/{reverse['id']}/rows?offset=0&limit=1").json()
        assert page["total"] == 2
        assert len(page["items"]) == 1
        evidence = api.get(url + f"/{reverse['id']}/rows").json()["items"]
        assert all(len(x["evidence"]) == 1 for x in evidence)
        assert all(x["also_in_other_direction"] for x in evidence)
        exported = api.get(url + f"/{forward['id']}/export")
        assert exported.status_code == 200
        assert "'=1+1" in exported.text
        assert "cooling blanket" in exported.text and "2026-10-17" in exported.text
        cached = api.post(
            url, json={"direction": "forward", "week_end": "2026-10-17", "max_rows": 100}
        ).json()
        assert cached["id"] == forward["id"]
        assert len(calls) == 2
        other_week = api.get(url + "?week_end=2026-10-10").json()
        assert other_week["complete"] is False
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        assert api.get(url + "?week_end=2026-10-17").json()["complete"] is True
        assert len(calls) == 2
        project = api.post("/api/v1/projects", json={"name": "other", "marketplace": "US"}).json()[
            "id"
        ]
        assert (
            api.get(url.replace(base.split("/")[4], project) + "/" + str(reverse["id"])).status_code
            == 404
        )


def test_forward_queries_download_separate_named_xlsx_files(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    base = prepare(db, engine, tmp_path)
    replace_core_keywords(engine, tmp_path, ["dumbbell rack", "weight/rack"])

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query = next(
            item["value"]
            for item in body["filterSelections"]
            if item["id"] == "search-term-freeform"
        )
        raw = row(f"{query} extension", 120000)
        raw.update(
            {
                "st-top-asin-click-share-1": "20%",
                "st-top-asin-conversion-share-1": "13%",
                "st-top-asin-2": "B000000002",
                "st-top-asin-conversion-share-2": "10%",
                "st-top-asin-3": "B000000003",
                "st-top-asin-conversion-share-3": "5%",
            }
        )
        return httpx.Response(200, json={"reportsV2": [{"totalItems": 1, "rows": [raw]}]})

    client = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    url = base + "/aba-expansion"
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        report = api.post(
            url, json={"direction": "forward", "week_end": "2026-10-17", "max_rows": 100}
        ).json()
        report = wait(api, url, report["id"])
        assert report["status"] == "succeeded"

        first = api.get(url + f"/{report['id']}/queries/0/export.xlsx")
        second = api.get(url + f"/{report['id']}/queries/1/export.xlsx")

        assert first.status_code == second.status_code == 200
        assert "filename*=UTF-8''%E7%AC%AC%E5%9B%9B%E6%AD%A5ABA%E6%AD%A3%E5%90%91dumbbell%20rack.xlsx" in first.headers["content-disposition"]
        assert "filename*=UTF-8''%E7%AC%AC%E5%9B%9B%E6%AD%A5ABA%E6%AD%A3%E5%90%91weight-rack.xlsx" in second.headers["content-disposition"]
        first_rows = [values for _, values in _xlsx(first.content)]
        second_rows = [values for _, values in _xlsx(second.content)]
        assert first_rows[2][1] == "dumbbell rack extension"
        assert second_rows[2][1] == "weight/rack extension"
        assert first_rows[2][2:4] == ["65.00%", "28.00%"]
        assert second_rows[2][2:4] == ["65.00%", "28.00%"]
        assert "weight/rack extension" not in str(first_rows)
        assert "dumbbell rack extension" not in str(second_rows)

        # 旧版中断快照没有逐查询完成状态；即使已有分页数据，也不能伪装成完整文件。
        with Repository(engine, tmp_path).connect() as conn:
            stored = conn.execute(
                "SELECT document FROM aba_expansions WHERE id=?", (report["id"],)
            ).fetchone()
            interrupted = json.loads(stored[0])
            interrupted["status"] = "interrupted"
            interrupted["queries"][0].pop("status", None)
            conn.execute(
                "UPDATE aba_expansions SET document=? WHERE id=?",
                (json.dumps(interrupted), report["id"]),
            )
        blocked = api.get(url + f"/{report['id']}/queries/0/export.xlsx")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "ABA_QUERY_EXPORT_INCOMPLETE"

        interrupted["status"] = "partial"
        interrupted["warnings"] = ["采集意外中断，请读取已保存进度后显式重试。"]
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute(
                "UPDATE aba_expansions SET document=? WHERE id=?",
                (json.dumps(interrupted), report["id"]),
            )
        legacy_interrupted = api.get(url + f"/{report['id']}/queries/0/export.xlsx")
        assert legacy_interrupted.status_code == 409
        assert legacy_interrupted.json()["error"]["code"] == "ABA_QUERY_EXPORT_INCOMPLETE"

        interrupted["warnings"] = [
            "部分查询失败或达到采集边界，当前结果不代表穷尽；请查看各查询原因后显式重试。"
        ]
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute(
                "UPDATE aba_expansions SET document=? WHERE id=?",
                (json.dumps(interrupted), report["id"]),
            )
        legacy_finished_partial = api.get(url + f"/{report['id']}/queries/0/export.xlsx")
        assert legacy_finished_partial.status_code == 200


def test_query_xlsx_rejects_out_of_range_and_empty_results(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    base = prepare(db, engine, tmp_path)
    client = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"reportsV2": [{"totalItems": 0, "rows": []}]}
            )
        ),
    )
    url = base + "/aba-expansion"
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        forward = api.post(
            url, json={"direction": "forward", "week_end": "2026-10-17"}
        ).json()
        forward = wait(api, url, forward["id"])
        reverse = api.post(
            url, json={"direction": "reverse", "week_end": "2026-10-17"}
        ).json()
        reverse = wait(api, url, reverse["id"])

        empty = api.get(url + f"/{forward['id']}/queries/0/export.xlsx")
        out_of_range = api.get(url + f"/{forward['id']}/queries/99/export.xlsx")
        reverse_empty = api.get(url + f"/{reverse['id']}/queries/0/export.xlsx")

        assert empty.status_code == 409
        assert empty.json()["error"]["code"] == "ABA_QUERY_EXPORT_EMPTY"
        assert out_of_range.status_code == 404
        assert reverse_empty.status_code == 409
        assert reverse_empty.json()["error"]["code"] == "ABA_QUERY_EXPORT_EMPTY"


def test_forward_collects_every_confirmed_core_keyword(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    keywords = [f"core keyword {index}" for index in range(1, 7)]
    replace_core_keywords(engine, tmp_path, keywords)
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(
            next(
                item["value"]
                for item in body["filterSelections"]
                if item["id"] == "search-term-freeform"
            )
        )
        return httpx.Response(200, json={"reportsV2": [{"totalItems": 0, "rows": []}]})

    url = base + "/aba-expansion"
    client = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        report = api.post(url, json={"direction": "forward", "week_end": "2026-10-17"}).json()
        result = wait(api, url, report["id"])
        parsed = ExpansionReport.model_validate(result)
        assert parsed.status == "succeeded"
        assert [item.query for item in parsed.queries] == keywords
        assert calls == keywords


def test_legacy_protocol_reports_are_history_not_current_completion(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    client = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"reportsV2": [{"totalItems": 0, "rows": []}]})
        ),
    )
    url = base + "/aba-expansion"
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        for direction in ("forward", "reverse"):
            report = api.post(url, json={"direction": direction, "week_end": "2026-10-17"}).json()
            assert wait(api, url, report["id"])["status"] == "succeeded"
        assert api.get(url + "?week_end=2026-10-17").json()["complete"] is True
        with Repository(engine, tmp_path).connect() as conn:
                rows = conn.execute("SELECT id, document FROM aba_expansions").fetchall()
                for expansion_id, document in rows:
                    legacy = json.loads(document)
                    legacy.pop("protocol_version", None)
                    conn.execute(
                        "UPDATE aba_expansions SET document=? WHERE id=?",
                        (json.dumps(legacy), expansion_id),
                    )
        workspace = api.get(url + "?week_end=2026-10-17").json()
        assert workspace["complete"] is False
        assert all(report["stale"] for report in workspace["reports"])


def test_failures_truncation_and_stale_cannot_complete(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    attempts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        query = next(x["value"] for x in body["filterSelections"] if x["id"] == "asins")
        attempts.append(query)
        return httpx.Response(
            200, json={"reportsV2": [{"totalItems": 10, "rows": [row("blanket", 10)]}]}
        )

    url = base + "/aba-expansion"
    client = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle), attempts=1)
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        response = api.post(
            url, json={"direction": "reverse", "week_end": "2026-10-17", "max_rows": 1}
        )
        assert response.status_code == 200, response.text
        report = wait(api, url, response.json()["id"])
        assert report["status"] == "partial"
        parsed = ExpansionReport.model_validate(report)
        assert parsed.queries[0].truncated is True
        assert attempts == ["B012345678,B087654321"]
        assert api.get(url + "?week_end=2026-10-17").json()["complete"] is False
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute("DELETE FROM root_selections")
        assert api.get(url + "/" + str(report["id"])).json()["stale"] is True
        assert (
            api.post(url, json={"direction": "reverse", "week_end": "2026-10-17"}).status_code
            == 409
        )
        assert (
            api.post(url, json={"direction": "forward", "week_end": "2026-10-16"}).status_code
            == 422
        )


def test_rank_conflicts_and_csv_cells_preserve_safe_evidence(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        first = row("same keyword", 10)
        second = row("same keyword", 20)
        first["st-top-asin-1"] = "=1+1"
        second["st-top-asin-1"] = "=1+1"
        return httpx.Response(200, json={"reportsV2": [{"totalItems": 2, "rows": [first, second]}]})

    client = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    url = base + "/aba-expansion"
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        report = api.post(url, json={"direction": "reverse", "week_end": "2026-10-17"}).json()
        wait(api, url, report["id"])
        item = api.get(url + f"/{report['id']}/rows").json()["items"][0]
        assert item["rank_conflict"] is True
        assert item["search_frequency_rank"] is None
        assert {e["search_frequency_rank"] for e in item["evidence"]} == {10, 20}
        assert "raw_rows" not in str(item)
        rows = list(csv.reader(io.StringIO(api.get(url + f"/{report['id']}/export").text)))
        assert rows[1][6] == "'=1+1"


def test_duplicate_keyword_in_one_query_preserves_conflicting_evidence(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    client = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "reportsV2": [
                        {"totalItems": 2, "rows": [row("blanket", 1), row(" Blanket ", 2)]}
                    ]
                },
            )
        ),
    )
    url = base + "/aba-expansion"
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        report = api.post(url, json={"direction": "forward", "week_end": "2026-10-17"}).json()
        assert wait(api, url, report["id"])["status"] == "partial"
        item = api.get(url + f"/{report['id']}/rows").json()["items"][0]
        assert item["rank_conflict"] is True
        assert {e["search_frequency_rank"] for e in item["evidence"]} == {1, 2}
        with Repository(engine, tmp_path).connect() as conn:
            import json

            result = conn.execute(
                "SELECT results FROM aba_expansions WHERE id=?", (report["id"],)
            ).fetchone()
            assert len(json.loads(result[0])[0]["raw_rows"]) == 2


def test_running_dedup_context_change_and_interrupted_recovery(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    entered, release = Event(), Event()
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={"reportsV2": [{"totalItems": 0, "rows": []}]})

    client = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    url = base + "/aba-expansion"
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        payload = {"direction": "reverse", "week_end": "2026-10-17"}
        report = api.post(url, json=payload).json()
        try:
            assert entered.wait(2)
            assert api.post(url, json=payload).json()["id"] == report["id"]
            assert len(calls) == 1
            with Repository(engine, tmp_path).connect() as conn:
                conn.execute("DELETE FROM root_selections")
        finally:
            release.set()
        result = wait(api, url, report["id"])
        assert result["stale"] is True
        assert result["status"] == "partial"
        assert len(calls) == 1
        assert not api.get(url).json()["complete"]
    # 模拟进程退出时留下的运行中快照，重启只标中断，不自动采集。
    with Repository(engine, tmp_path).connect() as conn:
        row = conn.execute(
            "SELECT document FROM aba_expansions WHERE id=?", (report["id"],)
        ).fetchone()
        interrupted = json.loads(row[0])
        interrupted["status"] = "running"
        conn.execute(
            "UPDATE aba_expansions SET document=? WHERE id=?",
            (json.dumps(interrupted), report["id"]),
        )
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        assert api.get(url + "/" + report["id"]).json()["status"] == "interrupted"
        assert len(calls) == 1


def test_empty_success_and_failed_explicit_retry(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(403)
        return httpx.Response(200, json={"reportsV2": [{"totalItems": 0, "rows": []}]})

    url = base + "/aba-expansion"
    client = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle), attempts=1)
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        payload = {"direction": "forward", "week_end": "2026-10-17"}
        first = api.post(url, json=payload).json()
        assert wait(api, url, first["id"])["status"] == "failed"
        assert api.get(url).status_code == 200 and len(calls) == 1
        retry = api.post(url, json=payload).json()
        assert retry["id"] != first["id"]
        assert wait(api, url, retry["id"])["status"] == "succeeded"
        assert api.get(url + f"/{retry['id']}/rows").json()["total"] == 0
        assert len(calls) == 2


def test_later_page_failure_retains_first_page_progress(db: str, engine: Engine, tmp_path: Path) -> None:
    import json

    base = prepare(db, engine, tmp_path)
    second_page, release = Event(), Event()

    def handle(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["reportOperations"][0]["pageNumber"]
        if page == 2:
            second_page.set()
            assert release.wait(5)
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "reportsV2": [
                    {"totalItems": 200, "rows": [row(f"term {i}", i + 1) for i in range(100)]}
                ]
            },
        )

    url = base + "/aba-expansion"
    client = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle), attempts=1)
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=client)) as api:
        report = api.post(url, json={"direction": "forward", "week_end": "2026-10-17"}).json()
        try:
            assert second_page.wait(2)
            progress = api.get(url + "/" + report["id"]).json()
            assert progress["status"] == "running"
            assert progress["queries"][0]["fetched"] == 100
            assert api.get(url + f"/{report['id']}/rows").json()["total"] == 100
            incomplete = api.get(url + f"/{report['id']}/queries/0/export.xlsx")
            assert incomplete.status_code == 409
            assert incomplete.json()["error"]["code"] == "ABA_QUERY_EXPORT_INCOMPLETE"
        finally:
            release.set()
        result = wait(api, url, report["id"])
        parsed = ExpansionReport.model_validate(result)
        assert parsed.status == "partial"
        assert parsed.queries[0].error_code == "ABA_ACCESS_DENIED"
        assert parsed.queries[0].fetched == 100
        assert api.get(url + f"/{report['id']}/rows").json()["total"] == 100
