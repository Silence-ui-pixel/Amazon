"""卖家精灵接口直取仍保存 XLSX，并按业务方向生成中文文件名。"""

import json
from pathlib import Path

import pytest

from app.adapters.sellersprite_browser import (
    download_extension_via_api,
    download_keywords_via_api,
)
from app.domain.sellersprite import BrowserCollectionInput


class Page:
    def evaluate(self, _script: str, _payload: object) -> dict[str, object]:
        return {
            "status": 200,
            "text": json.dumps(
                {
                    "code": "OK",
                    "data": {
                        "items": [{"keyword": "rack", "keywords": "rack"}],
                        "total": 1,
                    },
                }
            ),
        }


@pytest.mark.parametrize(
    ("collection", "expected"),
    [
        (
            BrowserCollectionInput(
                direction="forward", method="keyword_mining", query="dumbbell/rack"
            ),
            "卖家精灵正向dumbbell-rack.xlsx",
        ),
        (
            BrowserCollectionInput(
                direction="reverse", method="keyword_reverse", query="B012345678"
            ),
            "卖家精灵反向B012345678.xlsx",
        ),
        (
            BrowserCollectionInput(
                direction="reverse",
                method="traffic_extension",
                queries=["B012345678"],
                variant_scope="all",
            ),
            "卖家精灵反向B012345678.xlsx",
        ),
        (
            BrowserCollectionInput(
                direction="reverse",
                method="traffic_extension",
                queries=["B012345678", "B087654321"],
                variant_scope="all",
            ),
            "卖家精灵反向多ASIN.xlsx",
        ),
    ],
)
def test_api_collection_saves_xlsx_with_business_filename(
    tmp_path: Path, collection: BrowserCollectionInput, expected: str
) -> None:
    target = tmp_path / "original.xlsx"

    if collection.method == "traffic_extension":
        filename, raw, total = download_extension_via_api(Page(), collection, target)  # type: ignore[arg-type]
    else:
        filename, raw, total = download_keywords_via_api(Page(), collection, target)  # type: ignore[arg-type]

    assert filename == expected
    assert total == 1
    assert raw.startswith(b"PK")
    assert target.read_bytes() == raw
    if len(collection.queries) > 1:
        assert all(asin not in filename for asin in collection.queries)
