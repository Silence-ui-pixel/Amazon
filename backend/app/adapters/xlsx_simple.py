"""最小 XLSX 生成：把二维表格打成单表工作簿。

卖家精灵的接口直取拿到的是 JSON，而下游解析器读的是供应商原件格式（xlsx 首行表头），
这里只负责把 JSON 还原成同样结构的单表文件，不带样式、公式和合并单元格。
"""

import io
import re
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# XML 1.0 不允许的控制字符，出现在关键词里会直接让文件损坏。
ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def column_letters(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _worksheet(rows: list[list[str]], width: int) -> bytes:
    root = Element("worksheet", xmlns=SHEET_NS)
    cols = SubElement(root, "cols")
    SubElement(cols, "col", min="1", max=str(max(width, 1)), width="18", customWidth="1")
    sheet = SubElement(root, "sheetData")
    for row_index, values in enumerate(rows, 1):
        row = SubElement(sheet, "row", r=str(row_index))
        for col_index, value in enumerate(values, 1):
            # inlineStr 让等号开头的文本保持字面量，不会被 Excel 当公式执行。
            cell = SubElement(
                row, "c", r=f"{column_letters(col_index)}{row_index}", t="inlineStr"
            )
            text = SubElement(SubElement(cell, "is"), "t")
            text.set(XML_SPACE, "preserve")
            text.text = ILLEGAL.sub("", str(value))
    return tostring(root, encoding="utf-8", xml_declaration=True)


def build_xlsx(rows: list[list[str]], *, sheet: str = "Sheet1") -> bytes:
    """rows 第一行是表头；空表也至少写出表头，避免解析器读不到列。"""
    if not rows:
        rows = [[""]]
    width = max(len(row) for row in rows)

    content = Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    SubElement(content, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    SubElement(content, "Default", Extension="xml", ContentType="application/xml")
    SubElement(
        content,
        "Override",
        PartName="/xl/workbook.xml",
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    )
    SubElement(
        content,
        "Override",
        PartName="/xl/worksheets/sheet1.xml",
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    )

    workbook = Element("workbook", xmlns=SHEET_NS)
    sheets = SubElement(workbook, "sheets")
    SubElement(
        sheets, "sheet", {"name": sheet, "sheetId": "1", f"{{{OFFICE_REL}}}id": "rId1"}
    )

    relationships = Element("Relationships", xmlns=REL_NS)
    SubElement(
        relationships,
        "Relationship",
        Id="rId1",
        Type=OFFICE_REL + "/worksheet",
        Target="worksheets/sheet1.xml",
    )

    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml", tostring(content, encoding="utf-8", xml_declaration=True)
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml", tostring(workbook, encoding="utf-8", xml_declaration=True)
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            tostring(relationships, encoding="utf-8", xml_declaration=True),
        )
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet(rows, width))
    return out.getvalue()
