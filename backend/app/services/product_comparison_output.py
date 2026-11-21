"""Validate comparisons independently; missing facts never become fabricated scores."""
import json

from pydantic import ValidationError

from app.domain.product_terms import BatchComparisonDraft

FACT_LABELS = {"purpose": "用途", "style_structure": "款式结构", "specifications": "规格"}


def parse_comparison_output(
    raw: str, asins: list[str]
) -> tuple[dict[str, BatchComparisonDraft], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return {}, [{"asin": asin, "reason": "未返回有效的商品结果数组"} for asin in asins]
    grouped: dict[str, list[dict[str, object]]] = {asin: [] for asin in asins}
    for item in payload["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("asin"), str):
            issues.append({"asin": "", "reason": "忽略未提供 ASIN 的多余结果"})
            continue
        asin = item["asin"]
        if asin not in grouped:
            issues.append({"asin": "", "reason": "忽略不属于请求 ASIN 的多余结果"})
            continue
        grouped[asin].append(item)
    results: dict[str, BatchComparisonDraft] = {}
    for asin, rows in grouped.items():
        if len(rows) != 1:
            issues.append({"asin": asin, "reason": "模型遗漏该 ASIN" if not rows else "模型重复返回该 ASIN"})
            continue
        item = dict(rows[0])
        if not isinstance(item.get("valid"), bool):
            issues.append({"asin": asin, "reason": "缺少明确的商品有效性判断"})
            continue
        missing: list[str] = []
        for key, label in FACT_LABELS.items():
            value = item.get(key)
            if isinstance(value, str):
                value = [value.strip()] if value.strip() else []
            elif isinstance(value, list) and all(isinstance(part, str) for part in value):
                value = [part.strip() for part in value if part.strip()]
            elif value is None:
                value = []
            item[key] = value
            if value == []:
                missing.append(label)
        if item["valid"] and missing:
            item.update(valid=False, scores=None, exclusion_code="PRODUCT_DATA_INCOMPLETE",
                        exclusion_reason="资料不足：" + "、".join(missing) + "缺失，未进入相关度筛选")
        reason = item.get("exclusion_reason")
        if (
            item["valid"] is False
            and missing
            and not item.get("exclusion_code")
            and isinstance(reason, str)
            and any(marker in reason.casefold() for marker in (
                "资料不足", "信息不足", "证据不足", "资料缺失", "信息缺失", "规格缺失",
                "未提供", "无法提取", "insufficient", "missing", "incomplete", "lack of information",
            ))
        ):
            item["exclusion_code"] = "PRODUCT_DATA_INCOMPLETE"
        try:
            results[asin] = BatchComparisonDraft.model_validate(item)
        except ValidationError as error:
            fields = sorted({".".join(str(part) for part in entry["loc"]) or "商品结果"
                             for entry in error.errors(include_input=False, include_context=False, include_url=False)})
            issues.append({"asin": asin, "reason": "输出字段不符合约定：" + "、".join(fields)[:250]})
    return results, issues
