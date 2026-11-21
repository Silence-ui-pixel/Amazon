"""文本归一化与词根包含判定；各步骤共用同一口径，避免同义词判断分叉。"""

import re
import unicodedata

# 只切拉丁字母与数字；标点和空白都算边界，不推断单复数或拼写变体。
_TERM = re.compile(r"[a-z0-9]+")


def phrase_key(value: str) -> str:
    """去重与相等判断的键：折叠空白并忽略大小写；不合并单复数或拼写变体。"""
    return " ".join(value.split()).casefold()


def fold_text(value: str) -> str:
    """比较用文本：先做 NFKC 归一化，再折叠空白与大小写。"""
    return phrase_key(unicodedata.normalize("NFKC", value))


def tokenize(value: str) -> tuple[str, ...]:
    """英文连续词切分；标点与空白是边界，不推断单复数。"""
    return tuple(_TERM.findall(fold_text(value)))


def contains_root(keyword: str, root: str) -> bool:
    """判断关键词是否含完整词根。

    拉丁书写系统按完整词序列判断，避免 wheel 误配 wheelbarrow；
    非拉丁书写系统没有空格分词，退回子串判断。
    """
    root_key = fold_text(root)
    if not root_key:
        return False
    if not root_key.isascii():
        return root_key in fold_text(keyword)
    terms = tokenize(keyword)
    wanted = tokenize(root)
    if not wanted:
        return False
    return any(
        terms[index : index + len(wanted)] == wanted
        for index in range(len(terms) - len(wanted) + 1)
    )
