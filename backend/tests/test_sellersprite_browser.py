from pathlib import Path

import pytest


def test_chinese_traffic_extension_page_without_html_lang_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.adapters.sellersprite_browser as browser

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def get_attribute(self, name: str) -> None:
            assert self.selector == "html" and name == "lang"
            return None

        def count(self) -> int:
            return 0

        @property
        def first(self) -> "Locator":
            return self

        def inner_text(self) -> str:
            assert self.selector == "body"
            return "亚马逊流量词拓展工具 最近30天 立即查询"

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

        def wait_for_timeout(self, _: int) -> None:
            pass

    ticks = iter([0.0, 21.0])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(ticks, 21.0))

    browser.ensure_chinese(Page())  # type: ignore[arg-type]


def test_v3_chinese_page_is_accepted_without_legacy_button_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拓展流量词 v3 页：html lang 是 en，也没有“立即查询/最近30天”，但界面是中文。"""
    import app.adapters.sellersprite_browser as browser

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def get_attribute(self, name: str) -> str | None:
            assert self.selector == "html" and name == "lang"
            return "en"

        def count(self) -> int:
            return 0

        @property
        def first(self) -> "Locator":
            return self

        def inner_text(self) -> str:
            assert self.selector == "body"
            return "拓展流量词 全部变体 请输入 ASIN 历史记录 导出结果 数据更新时间"

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

        def wait_for_timeout(self, _: int) -> None:
            pass

    ticks = iter([0.0, 21.0])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(ticks, 21.0))

    browser.ensure_chinese(Page())  # type: ignore[arg-type]


def test_english_page_is_not_mistaken_for_chinese(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.adapters.sellersprite_browser as browser
    from app.domain.errors import DomainError

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def get_attribute(self, name: str) -> str | None:
            assert self.selector == "html" and name == "lang"
            return "en"

        def count(self) -> int:
            return 0

        @property
        def first(self) -> "Locator":
            return self

        def inner_text(self) -> str:
            assert self.selector == "body"
            return "Traffic Keyword Extension Enter ASIN History Export 简体"

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

        def wait_for_timeout(self, _: int) -> None:
            pass

    ticks = iter([0.0, 21.0])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(ticks, 21.0))

    with pytest.raises(DomainError) as caught:
        browser.ensure_chinese(Page())  # type: ignore[arg-type]
    assert caught.value.code == "SELLERSPRITE_LANGUAGE_UNSUPPORTED"


def test_period_select_is_found_by_value_not_position() -> None:
    """页面改版后下拉顺序变化，按当前值仍能定位最近30天，而不是写死第 2 个。"""
    from app.adapters.sellersprite_browser import PERIOD_LABELS, find_select_with_value

    class Input:
        def __init__(self, value: str) -> None:
            self.value = value

        def input_value(self) -> str:
            return self.value

    class Select:
        def __init__(self, value: str) -> None:
            self.value = value

        def locator(self, selector: str) -> Input:
            assert selector == "input"
            return Input(self.value)

    class SelectList:
        def __init__(self) -> None:
            self.items = [Select("美国站"), Select("全部类目"), Select("最近30天")]

        def count(self) -> int:
            return len(self.items)

        def nth(self, index: int) -> Select:
            return self.items[index]

    class Page:
        def locator(self, selector: str) -> SelectList:
            assert selector == ".el-select:visible"
            return SelectList()

    found = find_select_with_value(Page(), PERIOD_LABELS)  # type: ignore[arg-type]
    assert found is not None and found.locator("input").input_value() == "最近30天"


def test_traffic_extension_asins_are_filled_once_as_one_space_separated_batch() -> None:
    from app.adapters.sellersprite_browser import fill_traffic_extension_asins

    class Input:
        def __init__(self) -> None:
            self.values: list[str] = []

        def fill(self, value: str) -> None:
            self.values.append(value)

        def input_value(self) -> str:
            return self.values[-1]

    asin_input = Input()
    fill_traffic_extension_asins(
        asin_input,  # type: ignore[arg-type]
        ["B0HGB5ZR95", "B0HGYZ44CR", "B0DRVWW5B3"],
    )

    assert asin_input.values == ["B0HGB5ZR95 B0HGYZ44CR B0DRVWW5B3"]


def test_custom_columns_with_surrounding_whitespace_selects_every_available_column_before_export() -> (
    None
):
    from app.adapters.sellersprite_browser import select_all_custom_columns

    class Checkbox:
        def __init__(self, checked: bool) -> None:
            self.checked = checked

        def is_enabled(self) -> bool:
            return True

        def is_checked(self) -> bool:
            return self.checked

        def check(self, *, force: bool) -> None:
            assert force is True
            self.checked = True

    class CheckboxList:
        def __init__(self) -> None:
            self.items = [Checkbox(True), Checkbox(False), Checkbox(False), Checkbox(False)]

        @property
        def first(self) -> Checkbox:
            return self.items[0]

        def count(self) -> int:
            return len(self.items)

        def nth(self, index: int) -> Checkbox:
            return self.items[index]

    class Button:
        def __init__(self) -> None:
            self.clicked = False

        @property
        def first(self) -> "Button":
            return self

        def count(self) -> int:
            return 1

        def click(self, **_: object) -> None:
            self.clicked = True

    class MissingButton(Button):
        def count(self) -> int:
            return 0

        def click(self, **_: object) -> None:
            raise AssertionError("button is not available by accessible name")

    class ButtonCandidates:
        def __init__(self, button: Button, text: str) -> None:
            self.button = button
            self.text = text

        @property
        def first(self) -> Button:
            return self.button

        def filter(self, **options: object) -> "ButtonCandidates":
            pattern = options["has_text"]
            button = self.button if pattern.search(self.text) else MissingButton()  # type: ignore[union-attr]
            return ButtonCandidates(button, self.text)

    class Dialog:
        def __init__(self, save: Button, checkboxes: CheckboxList) -> None:
            self.save = save
            self.checkboxes = checkboxes
            self.hidden = False

        @property
        def last(self) -> "Dialog":
            return self

        def filter(self, **options: object) -> "Dialog":
            pattern = options["has_text"]
            assert pattern.search("自定义列\n全选\n相关产品\n保存")
            return self

        def wait_for(self, *, state: str = "visible", **_: object) -> None:
            if state == "hidden":
                assert self.save.clicked
                self.hidden = True

        def locator(self, selector: str) -> CheckboxList | ButtonCandidates:
            if selector == "button:visible":
                return ButtonCandidates(self.save, " \N{NO-BREAK SPACE}保存 ")
            assert selector == 'input[type="checkbox"]'
            return self.checkboxes

        def get_by_role(self, role: str, **_: object) -> Button:
            assert role == "button"
            return MissingButton()

    class Page:
        def __init__(self) -> None:
            self.open = Button()
            self.save = Button()
            self.checkboxes = CheckboxList()
            self.dialog = Dialog(self.save, self.checkboxes)

        def get_by_role(self, role: str, **options: object) -> Button:
            assert role == "button"
            assert "name" in options
            return MissingButton()

        def locator(self, selector: str) -> Dialog | ButtonCandidates:
            if selector == "button:visible":
                return ButtonCandidates(self.open, " 自定义列 ")
            assert "dialog" in selector
            return self.dialog

        def wait_for_timeout(self, milliseconds: int) -> None:
            assert milliseconds == 200

    page = Page()
    select_all_custom_columns(page)  # type: ignore[arg-type]

    assert page.open.clicked is True
    assert page.save.clicked is True
    assert page.dialog.hidden is True
    assert all(item.checked for item in page.checkboxes.items)


def test_custom_columns_waits_for_toolbar_render_before_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结果数先出现、工具栏后渲染：不能第一次查找不到就判定页面没有自定义列。"""
    import app.adapters.sellersprite_browser as browser
    from app.domain.errors import DomainError

    class Opener:
        def count(self) -> int:
            return 0

    class Candidates:
        @property
        def first(self) -> Opener:
            return Opener()

        def filter(self, **_: object) -> "Candidates":
            return self

    class Page:
        def __init__(self) -> None:
            self.waits: list[int] = []

        def locator(self, selector: str) -> Candidates:
            assert selector == "button:visible"
            return Candidates()

        def wait_for_timeout(self, milliseconds: int) -> None:
            self.waits.append(milliseconds)

    ticks = iter([0.0, 1.0, 30.0])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(ticks, 30.0))
    page = Page()

    with pytest.raises(DomainError) as caught:
        browser.select_all_custom_columns(page)  # type: ignore[arg-type]

    assert caught.value.code == "SELLERSPRITE_CUSTOM_COLUMNS_MISSING"
    assert page.waits == [1000]


def test_sop_collection_routes_and_variant_labels() -> None:
    from app.adapters.sellersprite_browser import collection_path, variant_button_label

    assert collection_path("keyword_mining") == "/v3/keyword-miner"
    assert collection_path("keyword_reverse") == "/v3/keyword-reverse"
    assert collection_path("traffic_extension") == "/v3/traffic/extend"
    assert variant_button_label("all") == "用全部变体拓词"
    assert variant_button_label("best_selling") == "用畅销变体拓词"
    assert variant_button_label("current") == "用当前变体拓词"


def test_traffic_extension_filename_is_scoped_to_selected_asins() -> None:
    from app.adapters.sellersprite_browser import export_filename_matches_request
    from app.domain.sellersprite import BrowserCollectionInput

    request = BrowserCollectionInput(
        direction="reverse",
        method="traffic_extension",
        queries=["B012345678", "B087654321"],
        variant_scope="all",
    )
    # 卖家精灵真实导出名只带首个 ASIN 和批量条数，如
    # ExpandKeywords-US-B0HGB5ZR95-batch(19)-202611-25095.xlsx。
    assert export_filename_matches_request(
        "ExpandKeywords-US-B012345678-batch(2)-202611-25095.xlsx", request
    )
    assert not export_filename_matches_request(
        "ExpandKeywords-US-B087654321-batch(2)-202611-25095.xlsx", request
    )
    assert not export_filename_matches_request(
        "ExpandKeywords-US-B012345678-batch(3)-202611-25095.xlsx", request
    )
    assert not export_filename_matches_request(
        "ExpandKeywords-DE-B012345678-batch(2)-202611-25095.xlsx", request
    )
    assert not export_filename_matches_request(
        "ExpandKeywords-US-B012345678-batch(2)-202611-25095.csv", request
    )
    # 旧版假设的 TrafficExtension 名字不是供应商真实产物，不再接受。
    assert not export_filename_matches_request(
        "TrafficExtension-US-B012345678-B087654321-Last-30-days-123.xlsx", request
    )


def test_sellersprite_export_filename_is_scoped() -> None:
    from app.adapters.sellersprite_browser import export_filename_matches

    assert export_filename_matches(
        "KeywordMining-US-gate-wheel-Last-30-days-700075.xlsx", "gate wheel", "forward"
    )
    assert not export_filename_matches(
        "KeywordMining-US-other-Last-30-days-700075.xlsx", "gate wheel", "forward"
    )


def test_recent_export_rejects_monthly_file() -> None:
    from app.adapters.sellersprite_browser import export_filename_matches, recent_export_matches

    assert recent_export_matches("ReverseASIN-US-B0CGV33WDY-Last-30-days-123.xlsx")
    assert recent_export_matches("KeywordMining-US-gate-wheel-Last-30-days-700075.xlsx")
    assert not recent_export_matches("ReverseASIN-US-B0CGV33WDY-202609.xlsx")
    assert export_filename_matches(
        "ReverseASIN-US-B0CGV33WDY-Last-30-days-123.xlsx", "B0CGV33WDY", "reverse"
    )
    assert not export_filename_matches(
        "ReverseASIN-US-B0CGV33WDY-Last-30-days-123.csv", "B0CGV33WDY", "reverse"
    )


def test_real_sellersprite_workbook_with_auxiliary_sheets(tmp_path: Path) -> None:
    import io
    from zipfile import ZipFile

    from app.adapters.third_party import map_rows, read_table, suggest

    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="US-gate wheel(1)_" r:id="r1"/><sheet name="Unique Words" r:id="r2"/><sheet name="Notes" r:id="r3"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Target="worksheets/sheet2.xml"/><Relationship Id="r3" Target="worksheets/sheet3.xml"/></Relationships>',
        )
        for i, rows in enumerate(
            [
                [["关键词", "PPC竞价", "#1 点击共享"], ["gate wheel", "$2.18", "0.1146"]],
                [["词语", "出现频次", "百分比"], ["gate", "1", "1"]],
                [["卖家精灵官网：https://www.sellersprite.com"]],
            ],
            1,
        ):
            body = "".join(
                '<row r="'
                + str(n)
                + '">'
                + "".join(
                    '<c r="'
                    + chr(65 + j)
                    + str(n)
                    + '" t="inlineStr"><is><t>'
                    + v
                    + "</t></is></c>"
                    for j, v in enumerate(row)
                )
                + "</row>"
                for n, row in enumerate(rows, 1)
            )
            z.writestr(
                f"xl/worksheets/sheet{i}.xml",
                "<worksheet><sheetData>" + body + "</sheetData></worksheet>",
            )
    table = read_table("real.xlsx", buffer.getvalue())
    assert len(table.rows) == 1
    rows = map_rows(table, suggest(table.headers), "fraction", currency="USD")
    assert str(rows[0].bid) == "2.18"
    assert rows[0].raw["PPC竞价"] == "$2.18"
    assert str(rows[0].top1_click_share) == "0.1146"
    from app.domain.errors import DomainError

    with pytest.raises(DomainError):
        map_rows(table, suggest(table.headers), "fraction", currency="EUR")


def test_traffic_export_workbook_accepts_asin_auxiliary_sheet() -> None:
    """拓展流量词原件带主表 + Unique Words + Asin（批量清单）+ Notes，不能误判为多主表。"""
    import io
    from zipfile import ZipFile

    from app.adapters.third_party import read_table

    sheets = [
        ("US-B0DP3TGBQF(19)__", [["关键词", "关键词翻译"], ["dumbbell rack", "哑铃架"]]),
        ("Unique Words", [["词语", "出现频次", "百分比", "", ""], ["rack", "398", "0.1139", "", ""]]),
        ("Asin", [["ASIN", "", "", "", ""], ["B0DP3TGBQF", "", "", "", ""]]),
        ("Notes", [["卖家精灵官网：https://www.sellersprite.com", "", "", "", ""]]),
    ]
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(
                f'<sheet name="{name}" r:id="r{index}"/>'
                for index, (name, _) in enumerate(sheets, 1)
            )
            + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            "<Relationships>"
            + "".join(
                f'<Relationship Id="r{index}" Target="worksheets/sheet{index}.xml"/>'
                for index in range(1, len(sheets) + 1)
            )
            + "</Relationships>",
        )
        for index, (_, rows) in enumerate(sheets, 1):
            body = "".join(
                f'<row r="{n}">'
                + "".join(
                    f'<c r="{chr(65 + j)}{n}" t="inlineStr"><is><t>{value}</t></is></c>'
                    for j, value in enumerate(row)
                )
                + "</row>"
                for n, row in enumerate(rows, 1)
            )
            z.writestr(f"xl/worksheets/sheet{index}.xml", "<worksheet><sheetData>" + body + "</sheetData></worksheet>")

    table = read_table("ExpandKeywords-US-B0DP3TGBQF-batch(19)-202611-25095.xlsx", buffer.getvalue())
    assert table.headers == ["关键词", "关键词翻译"]
    assert table.rows == [["dumbbell rack", "哑铃架"]]


def test_reverse_export_duplicate_update_times_are_disambiguated() -> None:
    from app.adapters.third_party import read_table, suggest

    data = (
        "关键词,自然排名,自然排名页码,更新时间,广告排名,广告排名页码,更新时间,PPC价格\n"
        "gate wheel,7,1,natural time,27,2,ad time,$0.97\n"
    ).encode()
    table = read_table("reverse.csv", data)
    assert table.headers == [
        "关键词",
        "自然排名",
        "自然排名页码",
        "更新时间（自然排名）",
        "广告排名",
        "广告排名页码",
        "更新时间（广告排名）",
        "PPC价格",
    ]
    assert table.rows[0][3] == "natural time" and table.rows[0][6] == "ad time"
    assert suggest(table.headers)["bid"] == "PPC价格"
