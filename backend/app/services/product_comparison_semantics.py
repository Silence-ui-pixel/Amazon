"""Calibrate judgments without changing the product comparison JSON contract."""
import re
from app.domain.product_terms import BatchComparisonDraft
from app.services.product_comparison_output import parse_comparison_output

COMPARISON_VERSION = "product-comparison-calibrated-v2"
SCORING_RUBRIC = (
    "角色固定：reference 永远是本品，products 每项永远是搜索商品；不得交换双方的承重等事实。"
    "先比较明确事实再打分，理由必须支持分数。用途按实际存储或服务的对象和核心需求判断；"
    "仅同属储物、收纳、支架等上位类别不能判用途相近。存鞋与存哑铃核心用途不同，用途分应为0..29，"
    "不能因为都能收纳就给60分以上。核心用途一致时才给60..100，完全一致给90..100。"
    "款式结构比较实际承重方式、支撑和适配结构；仅层数或外形相同但结构用途不同应给0..59，"
    "不能仅因同为三层而给高分。规格按双方明确可比的指标比较，不能把宽度与承重混作同一规格，"
    "不能从材质常识推测承重。660磅与1000磅必须分别归属于正确的商品，不得写反。"
    "资料齐全但不相关的商品仍 valid=true，使用低分保留在有效样本分母中；"
    "只有主维度缺失或无法可靠比较才 valid=false，明确指出缺少的资料。"
    "输出沿用下文唯一 JSON 格式，不新增证据字段。"
)
CONFLICTS = {
    "purpose": ("核心用途不匹配", "核心用途不同", "核心用途完全不同", "用途完全不同", "用途不一致", "核心需求完全不同"),
    "style_structure": ("核心结构不匹配", "结构完全不同", "结构完全不匹配"),
    "specifications": ("规格完全不同", "规格完全不匹配", "规格无法比较", "没有可比规格"),
}
NEGATION = re.compile(r"(?:并非|并不|不是|不代表|没有|不存在|不能认为|不属于)[^，,。；;]{0,16}$")

def _explicit_conflict(reason: str, dimension: str) -> bool:
    for marker in CONFLICTS[dimension]:
        offset = 0
        while (position := reason.find(marker, offset)) >= 0:
            clause = re.split(r"[，,。；;]", reason[:position])[-1]
            if not NEGATION.search(clause):
                return True
            offset = position + len(marker)
    return False

def parse_calibrated_comparison_output(raw: str, asins: list[str]) -> tuple[dict[str, BatchComparisonDraft], list[dict[str, str]]]:
    results, issues = parse_comparison_output(raw, asins)
    for asin, row in list(results.items()):
        if not row.valid or row.scores is None:
            continue
        for dimension in CONFLICTS:
            score = getattr(row.scores, dimension)
            reason = getattr(row.scores, dimension + "_reason")
            if score >= 60 and _explicit_conflict(reason, dimension):
                del results[asin]
                issues.append({'asin':asin,'reason':dimension + " 理由明确描述不匹配，但分数仍>=60；请依据双方事实重新评分"})
                break
    return results, issues
