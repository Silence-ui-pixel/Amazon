"""生成和人工修改共用的候选质量检查；最终相关度仍由真实商品样本确定。"""

import re

from app.domain.product_understanding import ProductUnderstanding
from app.domain.roots import CandidateValidationIssue, RootDraft
from app.domain.text import fold_text

BROAD_SINGLE_WORDS = {
    "storage",
    "fitness",
    "exercise",
    "equipment",
    "organizer",
    "accessory",
    "accessories",
    "rack",
    "stand",
    "shelf",
    "holder",
    "weight",
    "home",
    "gym",
    "sport",
    "sports",
}
NEGATIVE_REASON = re.compile(
    r"不建议(?:单独)?(?:使用|采用|保留)|不推荐|不应(?:单独)?(?:作为|使用|保留)|"
    r"应(?:当)?(?:剔除|删除|移除)|建议(?:剔除|删除|移除)|不宜单独|不能单独(?:作为|使用)|(?:过于|太)泛|并非同类|主体(?:错误|不符)|"
    r"(?:not recommended|too broad|wrong product|should be (?:removed|excluded))",
    re.I,
)


def validate_candidates(
    candidates: list[RootDraft], product: ProductUnderstanding
) -> tuple[list[RootDraft], list[CandidateValidationIssue]]:
    accepted: list[RootDraft] = []
    issues: list[CandidateValidationIssue] = []
    seen: set[str] = set()
    category = fold_text(product.product_type)
    context = fold_text(product.product_name + " " + product.product_type)
    subject_context = (
        context if category in {"unknown", "未知", "健身器材", "收纳设备"} else category
    )
    storage_subject = bool(
        re.search(
            r"(?:rack|stand|holder|shelf|organizer)\b|(?:收纳架|存储架|置物架|支架|储物架|哑铃架)",
            subject_context,
        )
    )
    for candidate in candidates:
        key = fold_text(candidate.text)
        code, message = "", ""
        if key in seen:
            code, message = "DUPLICATE", "重复候选已合并"
        elif NEGATIVE_REASON.search(candidate.reason):
            code, message = "SELF_REJECTED", "推荐理由明确否定了该候选，请改用完整且准确的品类词"
        elif key in BROAD_SINGLE_WORDS and key != category:
            code, message = "TOO_BROAD", "泛词无法准确表达商品主体，请补全对象和品类"
        elif len(key.split()) > 5:
            code, message = "DESCRIPTIVE_PHRASE", "候选过长，请保留完整品类并去掉属性描述"
        elif storage_subject and not re.search(
            r"\b(?:rack|stand|holder|shelf|organizer|storage)\b|架|柜|收纳",
            key + " " + candidate.translation,
        ):
            code, message = (
                "WRONG_SUBJECT",
                "本品主体是收纳或支撑结构，该候选仅表示被收纳物或其他商品",
            )
        if code:
            issues.append(CandidateValidationIssue(text=candidate.text, code=code, message=message))
        else:
            accepted.append(candidate)
        seen.add(key)
    return accepted, issues
