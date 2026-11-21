"""Amazon 访问检查：详情页不能被搜索页结构规则误判。"""

from pathlib import Path

from app.services.amazon_search import BrowserSession


class NormalProductPage:
    """模拟有商品标题、无搜索结果容器，并含 Amazon 通用文案的详情页。"""

    def evaluate(self, expression: str) -> bool:
        assert "productTitle" in expression
        return False


def test_normal_product_page_is_not_treated_as_access_check(tmp_path: Path) -> None:
    session = BrowserSession(NormalProductPage(), "US", "10001", tmp_path)  # type: ignore[arg-type]

    session.check_access()
