"""用户的正式表格只输出具有有效 ABA 周排名的词。"""

import csv
import io
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from sqlalchemy import Engine
from test_library import ready_library


def test_ranked_template_excel_csv_leave_unranked_in_source(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, _database_url, request):
        draft = api.post(base + "/library", json=request).json()
        url = base + f"/library/{draft['id']}"
        table = api.get(url + "/template?ranked_only=true").json()
        assert [r[0] for r in table["rows"]] == ["portable cooling blankets"]
        assert table["rows"][0][2] == 200001
        excel = api.get(url + "/template.xlsx?ranked_only=true")
        with ZipFile(io.BytesIO(excel.content)) as archive:
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            keywords = [
                cell.text
                for row in sheet.findall("s:sheetData/s:row", namespace)
                if int(row.attrib["r"]) >= 12
                for cell in row.findall("s:c[@r='A" + row.attrib["r"] + "']/s:is/s:t", namespace)
            ]
            assert keywords == ["portable cooling blankets"]
        result = api.get(url + "/export?ranked_only=true")
        records = list(csv.DictReader(io.StringIO(result.text.lstrip("\ufeff"))))
        assert len(records) == 1
        assert records[0]["ABA排名"] == "200001"
        assert len(api.get(url).json()["entries"]) == 2
        assert len(api.get(url + "/template?ranked_only=false").json()["rows"]) == 2
