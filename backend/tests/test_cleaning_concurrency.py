"""第六步清洗分类的小批并发执行契约。"""

import threading
import time

from app.domain.cleaning import Frequency
from app.services import cleaning as cleaning_service


def test_cleaning_classification_uses_small_concurrent_batches() -> None:
    """大量词根应拆成小批并发处理，同时保持批次和词根顺序。"""
    classify_batches = getattr(cleaning_service, "_review_cleaning_batches", None)
    assert callable(classify_batches), "第六步尚未提供小批并发分类"

    lock = threading.Lock()
    active = 0
    max_active = 0
    batch_sizes: list[int] = []

    class Model:
        def review_cleaning(
            self, _product: object, frequencies: list[Frequency], _phrases: list[str]
        ) -> list[str]:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                batch_sizes.append(len(frequencies))
            time.sleep(0.05)
            with lock:
                active -= 1
            return [item.term for item in frequencies]

    roots = [
        Frequency(
            term=f"term{index:02d}",
            occurrences=1,
            phrase_count=1,
            density=1,
            suggestions=[],
        )
        for index in range(41)
    ]

    decisions = classify_batches(Model(), object(), roots)

    assert sorted(batch_sizes) == [1, 20, 20]
    assert max_active >= 2
    assert decisions == [
        [item.term for item in roots[:20]],
        [item.term for item in roots[20:40]],
        [item.term for item in roots[40:]],
    ]
