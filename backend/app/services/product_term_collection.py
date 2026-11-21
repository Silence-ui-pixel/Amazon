"""按自然顺序采集商品词前 N 个唯一商品及详情。"""

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal, Protocol

from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.verification import (
    CollectionResult,
    ProductCollectionFailure,
    SearchPage,
)
from app.services.search_policy import PAUSE_CODES

PRODUCT_RETRY_CODES = {"ASIN_PAGE_TIMEOUT", "ASIN_BROWSER_FAILED", "ZINIAO_CDP_UNAVAILABLE"}

# 每个商品词最多比对多少个自然商品。深度分析要逐个打开商品详情页，
# 是整条流程最慢的一步；验收时可把 KEYWORD_TERM_SAMPLE_LIMIT 调小。
SAMPLE_LIMIT = max(1, int(os.environ.get("KEYWORD_TERM_SAMPLE_LIMIT", "30") or "30"))
try:
    PRODUCT_CONCURRENCY = max(
        1, min(8, int(os.environ.get("KEYWORD_PRODUCT_CONCURRENCY", "6") or "6"))
    )
except ValueError:
    PRODUCT_CONCURRENCY = 3
try:
    # 主页搜索按关键词并行。每个关键词仍按页码顺序翻页，避免跳页或丢排名。
    SEARCH_CONCURRENCY = max(
        1, min(8, int(os.environ.get("KEYWORD_SEARCH_CONCURRENCY", "6") or "6"))
    )
except ValueError:
    SEARCH_CONCURRENCY = 3


class ProductSearchSession(Protocol):
    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage: ...

    def product(self, asin: str) -> ProductSnapshot: ...


def collect_natural_products(
    session: ProductSearchSession,
    term: str,
    limit: int = 30,
    evidence_prefix: str = "product-term",
    progress: Callable[[Literal["searching", "collecting"], int, int, int, int], None] | None = None,
) -> CollectionResult:
    seen: set[str] = set()
    unique: list[str] = []
    pages: list[SearchPage] = []
    raw_count = 0
    page_number = 1
    while len(unique) < limit:
        if progress:
            progress("searching", len(pages), len(unique), 0, 0)
        page = session.search(term, page_number, f"{evidence_prefix}-{page_number}")
        pages.append(page)
        raw_count += len(page.cards)
        for card in page.cards:
            if card.asin not in seen:
                seen.add(card.asin)
                unique.append(card.asin)
                if len(unique) == limit:
                    break
        if len(unique) == limit or not page.next_page:
            break
        page_number += 1

    products: list[ProductSnapshot] = []
    failures: list[ProductCollectionFailure] = []

    fetch_many = getattr(session, "products", None)
    def read_details() -> Iterator[tuple[str, ProductSnapshot | None, Exception | None]]:
        if callable(fetch_many):
            # 分块发布真实已返回的详情数量；不增加采集并发。
            size = PRODUCT_CONCURRENCY if progress else max(1, len(unique))
            for offset in range(0, len(unique), size):
                yield from fetch_many(unique[offset:offset + size])
            return
        # Generic/test sessions remain sequential; only providers with isolated
        # browser contexts opt into concurrent detail reads.
        for asin in unique:
            try:
                yield asin, session.product(asin), None
            except Exception as error:
                yield asin, None, error

    if progress:
        progress("collecting", len(pages), len(unique), 0, 0)
    for asin, product, error in read_details():
        # A single page can time out while the rest of the original sample succeeds.
        # Retry that same ASIN once; identity/access errors retain their existing path.
        if isinstance(error, DomainError) and error.code in PRODUCT_RETRY_CODES:
            try:
                product = session.product(asin)
                error = None
            except Exception as retry_error:
                product, error = None, retry_error
        if error is None and product is not None:
            products.append(product)
            if progress:
                progress("collecting", len(pages), len(unique), len(products), len(failures))
            continue
        if isinstance(error, DomainError) and error.code in PAUSE_CODES:
            raise error
        failures.append(
            ProductCollectionFailure(
                asin=asin,
                reason=(error.message if isinstance(error, DomainError) else str(error))[:500]
                or type(error).__name__,
                error_code=(error.code if isinstance(error, DomainError) else "PRODUCT_DETAILS_FAILED"),
            )
        )
        if progress:
            progress("collecting", len(pages), len(unique), len(products), len(failures))
    return CollectionResult(
        term=term,
        raw_natural_count=raw_count,
        unique_asins=unique,
        products=products,
        failures=failures,
        pages=pages,
    )


def collect_terms_parallel(
    opener: Callable[[], ProductSearchSession],
    closer: Callable[[ProductSearchSession], None],
    terms: list[str],
    *,
    limit: int = SAMPLE_LIMIT,
    evidence_prefix: Callable[[str], str] | None = None,
    workers: int = SEARCH_CONCURRENCY,
) -> dict[str, CollectionResult | Exception]:
    """每个关键词使用独立搜索会话并行采集；同一关键词的翻页仍保持顺序。"""
    pending = list(dict.fromkeys(term for term in terms if term))
    if not pending:
        return {}

    def collect_one(term: str) -> CollectionResult:
        session = opener()
        try:
            prefix = evidence_prefix(term) if evidence_prefix else "product-term"
            return collect_natural_products(session, term, limit, prefix)
        finally:
            closer(session)

    results: dict[str, CollectionResult | Exception] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as pool:
        futures = {pool.submit(collect_one, term): term for term in pending}
        for future in as_completed(futures):
            term = futures[future]
            try:
                results[term] = future.result()
            except Exception as error:
                results[term] = error
    return results
