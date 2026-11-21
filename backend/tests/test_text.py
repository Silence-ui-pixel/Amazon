"""文本口径：归一化、分词与词根包含的共用规则。"""

from app.domain.text import contains_root, fold_text, phrase_key, tokenize


def test_phrase_key_folds_case_and_whitespace_but_not_plurals() -> None:
    assert phrase_key("  Cooling   Blanket ") == "cooling blanket"
    assert phrase_key("wheel") != phrase_key("wheels")


def test_fold_text_normalizes_full_width_forms() -> None:
    assert fold_text("ＷＨＥＥＬ") == "wheel"


def test_tokenize_uses_punctuation_as_boundary() -> None:
    assert tokenize("cooling-blanket, for hot sleepers") == (
        "cooling",
        "blanket",
        "for",
        "hot",
        "sleepers",
    )


def test_contains_root_requires_whole_word_for_latin() -> None:
    assert contains_root("cooling blanket", "blanket")
    assert contains_root("gate wheel", "wheel")
    assert not contains_root("wheelbarrow", "wheel")
    # 标点视为词边界：带连字符的复合词仍算包含完整词根。
    assert contains_root("wheel-based cart", "wheel")


def test_contains_root_falls_back_to_substring_for_non_latin() -> None:
    assert contains_root("車輪付き台車", "車輪")
    assert not contains_root("台車", "車輪")


def test_contains_root_rejects_empty_root() -> None:
    assert not contains_root("blanket", "   ")
