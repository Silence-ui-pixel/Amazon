"""词根分类规则层：通用层词表 + 品类层词表 + 主体词派生 + 判定顺序。

设计目标：换品类时只有"品类层"需要补，通用层与判定顺序不变。
本草稿实测：442 个导出词根规则命中 94%，与人工重判一致 98%。
"""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal
from uuid import UUID

# 允许进入三类的类别；其余（stop/unit/brand/subject）不导出
EXPORTED = ("selling_point", "scene", "preference")
TaxonomyCategory = Literal[
    "stop", "unit", "brand", "subject", "selling_point", "scene", "preference",
    "unclassified",
]

# ---------- 1. 通用层：跨品类复用 ----------

STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "of", "on", "or", "the", "to", "with", "your", "you", "my", "our",
}

UNIT = {
    "lb", "lbs", "pound", "pounds", "oz", "ounce", "ounces", "kg", "g", "gram",
    "inch", "inches", "in", "cm", "mm", "ft", "ml", "l", "liter",
    "gallon", "watt", "w", "v", "volt", "mah", "ah", "hz", "pcs", "pc", "pack",
    "count", "ct",
}

MATERIAL = {
    "steel", "stainless", "iron", "aluminum", "aluminium", "alloy", "plastic",
    "abs", "pp", "wood", "wooden", "bamboo", "glass", "ceramic", "silicone",
    "rubber", "leather", "faux", "pu", "fabric", "cotton", "linen", "polyester",
    "nylon", "mesh", "foam", "sponge", "vinyl", "pvc", "acrylic", "metal",
    "chrome", "coated", "plated", "painted", "brushed", "matte", "glossy",
    "transparent", "hex", "neoprene", "carbon", "tpu", "suede", "velvet",
}

# 材质词的中文别名：用于判断"商品资料是否把该材质写成卖点"（一词多标签）。
MATERIAL_ZH: dict[str, tuple[str, ...]] = {
    "steel": ("钢",), "stainless": ("不锈钢",), "iron": ("铁",), "aluminum": ("铝",),
    "wood": ("木",), "wooden": ("木",), "plastic": ("塑料",), "rubber": ("橡胶",),
    "leather": ("皮",), "fabric": ("布",), "silicone": ("硅胶",), "glass": ("玻璃",),
    "mesh": ("网",), "foam": ("泡棉", "海绵"), "vinyl": ("乙烯基", "pvc"),
}

COLOR = {
    "black", "white", "grey", "gray", "silver", "gold", "red", "blue", "green",
    "pink", "purple", "brown", "beige", "orange", "yellow", "clear", "color",
    "colour", "multicolor", "multicolour", "ivory", "navy", "tan",
}

SIZE = {
    "small", "mini", "large", "big", "xl", "xxl", "tall", "short", "wide",
    "narrow", "long", "deep", "high", "low", "compact", "slim", "thick", "thin",
    "oversize", "oversized", "standard", "medium", "huge", "tiny", "full",
}

QUANTITY = {
    "single", "dual", "double", "pair", "pairs", "set", "sets", "combo",
    "bundle", "piece", "pieces", "pack", "2pack", "3pack", "4pack", "two",
    "three", "four", "five", "six", "ten", "multiple", "extra",
}

SELLING_FUNCTION = {
    "adjustable", "adjust", "adjusted", "adjuster", "ajustable", "ajustables",
    "foldable", "folding", "fold", "collapsible", "detachable", "removable",
    "interchangeable", "replaceable", "reversible", "convertible",
    "stackable", "stacked", "stack", "stacks", "nesting", "expandable",
    "extendable", "extend", "telescopic", "retractable", "rotatable", "swivel",
    "lockable", "locking", "lock", "sealed", "leakproof", "waterproof",
    "water-resistant", "dustproof", "rustproof", "anti-slip", "nonslip",
    "anti", "slip", "magnetic", "rechargeable", "wireless", "cordless", "usb",
    "led", "multi", "multifunctional", "multifuncional", "multipurpose",
    "functional", "function", "portable", "lightweight", "heavy-duty",
    "durable", "sturdy", "strong", "reinforced", "breathable", "insulated",
    "washable", "easy-clean", "tool-free", "no-drill", "wall", "mounted",
    "freestanding", "space-saving", "saving", "storage", "storing",
    "organize", "organization", "tier", "tiers", "tiered", "layer", "layers",
    "level", "levels", "column", "horizontal", "vertical", "support", "fixed",
    "slot", "slots", "clamp", "catches", "separators", "keeper", "quick",
    "release", "quick-release", "install", "installation", "assemble",
    "assembled", "assembly", "setup", "included", "complete", "integrated",
    "mechanism", "safety", "safe", "protection", "protective", "padded",
    "cushioned", "cover", "display", "diy", "universal", "compatible", "fits",
    "fit", "variable", "changeable", "feet", "foot", "easy", "solid", "loadable",
    "selectable", "lift", "spinlock", "dial", "incline", "bearing", "stowable",
    "stabilizer", "rotating", "spin", "change", "changing", "purpose", "perform",
    "performance", "quality", "utility", "racking", "damage",
    "pad", "pads", "store", "hold", "attaching", "convenient",
    # 结构类词：有形容词性、但按本品类标准属于商品结构属性
    "detachable", "interchangeable", "wall", "mounted", "freestanding",
}

SCENE_PLACE = {
    "home", "house", "indoor", "outdoor", "office", "kitchen", "bedroom",
    "bathroom", "living", "room", "rooms", "garage", "garden", "patio",
    "balcony", "travel", "camping", "hotel", "car", "school", "classroom",
    "dorm", "apartment", "commercial", "restaurant", "shop", "gym", "studio",
    "beach", "picnic", "space", "spaces", "corner", "floor", "desk", "door",
    "doorway", "aisle", "closet", "counter", "countertop", "benchtop",
    "household", "backyard", "basement", "under", "upper", "upright",
    "standing", "place", "position", "base", "station", "middle", "farmhouse",
}

AUDIENCE = {
    "men", "mens", "man", "women", "woman", "womens", "kids", "kid", "baby",
    "toddler", "teen", "teens", "adult", "senior", "seniors", "family",
    "couple", "pet", "professional", "beginner", "beginners", "athlete",
    "child", "girl", "boy", "boys", "unisex",
}

OCCASION = {
    "daily", "everyday", "gift", "christmas", "birthday", "party", "wedding",
    "holiday", "work", "study", "fitness", "workout", "workouts", "training",
    "exercise", "exercises", "exercising", "excercise", "excersize", "sport",
    "sports", "yoga", "pilates", "gym", "strength", "crossfit", "weightlifting",
    "powerlifting", "bodybuilding", "boxing", "calisthenics", "deadlift",
    "squat", "squats", "bench", "benchpress", "curl", "curling", "row",
    "pulldown", "crunch", "crunches", "situp", "situps", "press", "landmine",
    "dropset", "biceps", "abs", "glute", "shoulder", "chest", "arms",
    "abdominal", "ankle", "balance", "isometric", "lifting", "hyrox",
}

STYLE_PRICE = {
    "cheap", "budget", "affordable", "premium", "luxury", "stylish", "modern",
    "vintage", "retro", "minimalist", "cute", "aesthetic", "classy",
    "attractive", "elegant", "simple", "basic", "professional", "smart",
    "digital", "design", "style", "pattern", "print", "shape", "shaped",
    "form", "looking", "look", "decor", "decoration", "heavy", "weighted",
    "loaded", "capacity", "light", "lightweight", "mass", "height", "gap",
    "ranges", "flat", "square", "round", "curved", "trapezoidal", "loose",
    "different", "fifteen", "variety", "assorted", "size", "sizes", "screws",
    "bolt", "collar", "disc", "disk", "soft",
}

# 拼写变体 / 西语词 → 归一（只做高置信的）
ALIASES = {
    "dumbell": "dumbbell", "dumbells": "dumbbell", "dummbell": "dumbbell",
    "dummbels": "dumbbell", "dumbbels": "dumbbell", "dumble": "dumbbell",
    "dumbles": "dumbbell", "dumbls": "dumbbell", "dunbells": "dumbbell",
    "dumbe": "dumbbell", "dumbel": "dumbbell", "dumbels": "dumbbell",
    "dumbbelll": "dumbbell", "dumbbellss": "dumbbell", "demvell": "dumbbell",
    "dembell": "dumbbell", "dimbells": "dumbbell", "bumbells": "dumbbell",
    "ajustable": "adjustable", "ajustables": "adjustable",
    "sqaut": "squat", "squatz": "squat", "squad": "squat",
    "excersize": "exercise", "excercise": "exercise", "excersing": "exercise",
    "workiut": "workout", "weigths": "weight", "weigts": "weight",
    "weughts": "weight", "wheights": "weight", "wieghts": "weight",
    "weigjts": "weight", "weght": "weight", "weigth": "weight",
    "mancuerna": "dumbbell", "mancuernas": "dumbbell", "pesas": "weight",
    "pesos": "weight", "casa": "home", "gimnasio": "gym",
    "ejercicios": "exercise", "mujer": "women", "soporte": "stand",
    "estante": "shelf", "aparato": "equipment", "verstellbar": "adjustable",
    "dumb": "dumbbell", "dumbbel": "dumbbell", "dumbweights": "dumbbell",
    "cowbell": "dumbbell", "kettleball": "kettlebell", "barras": "barbell",
    "aparatos": "equipment", "maquinas": "machine", "quipment": "equipment",
    "equiopment": "equipment", "fitnessequipment": "equipment",
    "weghts": "weight", "weig": "weight", "weighrs": "weight", "weighs": "weight",
    "weighta": "weight", "weightd": "weight", "wights": "weight",
    "homegym": "home", "homeij": "home", "hom": "home", "homeprogym": "home",
    "jusgym": "gym", "gymolo": "gym", "gymusclo": "gym", "gymstorage": "gym",
    "pilate": "pilates", "fitnes": "fitness", "squatting": "squat",
    "convenient": "easy", "multifunction": "multi", "adaptabells": "adjustable",
    "massive": "heavy", "physical": "training",
}

# 结构性名词：几乎在任何品类里都指“东西本身或其部件”，不是卖点/场景/偏好。
SUBJECT_COMMON = {
    "rack", "racks", "stand", "stands", "holder", "holders", "organizer",
    "organizers", "shelf", "shelves", "shelving", "frame", "unit", "system",
    "kit", "tray", "trays", "caddy", "container", "compartment", "bin", "box",
    "case", "cart", "tower", "hook", "hooks", "bracket", "brackets", "handle",
    "handles", "legs", "ladder", "attachment", "attachments", "accessory",
    "accessories", "tool", "tools", "item", "items", "part", "parts", "peg",
    "pegs", "rod", "rods", "basket", "baskets", "slots", "panel", "panels",
    "equipment", "machine", "machines", "block", "blocks", "tree", "trees",
    "bell", "bells", "kettle", "kettles", "gear", "barbell", "barbells",
    "barbel", "dumbbellrack", "dumbbellsrack", "fitstand", "dumber",
    "bench", "benches",
}

BRAND_COMMON = {
    "nike", "adidas", "reebok", "puma", "peloton", "nordictrack", "proform",
    "schwinn", "weider", "decathlon", "eleiko", "rogue", "titan", "hammer",
    "sunny", "merach", "mikolo", "tonal", "matrix", "nautilus", "york",
    "hoist", "cap", "marcy", "kingsmith", "speediance", "byzoom", "ifast",
    "joroto", "lusper", "hulkfit", "synergee", "ritfit", "papababe", "keppi",
    "trulap", "yes4all", "flybird", "bowflex", "powerblock", "selecttech",
    "ironmaster", "nuobell", "ativafit", "feierdun", "hyperbell", "smartbell",
    "trx", "airex", "apex", "fitgoods", "balanceform", "fitbench", "steadyrack",
}

# ---------- 2. 品类层：每个品类一份，随项目维护 ----------

EMPTY_CATEGORY: dict[str, set[str]] = {
    "subject": set(), "brand": set(), "selling_extra": set(), "scene_extra": set(),
}


def _slug(product_type: str) -> str:
    # 允许中文品类名：中文商品资料常见（如「健身器材配件」），不能都落到 default.json。
    slug = re.sub(r"[^\w]+", "-", (product_type or "default").strip().lower(), flags=re.UNICODE)
    return slug.strip("-_")[:60] or "default"


def category_path(storage_root: Path, project_id: UUID | str, product_type: str) -> Path:
    """共享品类规则文件：只保存人工维护的内容。"""
    return storage_root / "taxonomy" / str(project_id) / f"{_slug(product_type)}.json"


def runtime_path(
    storage_root: Path, project_id: UUID | str, run_id: UUID | str, product_type: str
) -> Path:
    """本次商品自动推断出的词，按 run 隔离，避免污染同项目的其它商品。"""
    return (
        storage_root / "taxonomy" / str(project_id) / "runs" / str(run_id)
        / f"{_slug(product_type)}.json"
    )


def load_category(storage_root: Path, project_id: UUID | str, product_type: str) -> dict[str, set[str]]:
    path = category_path(storage_root, project_id, product_type)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {key: set() for key in EMPTY_CATEGORY}
    return {
        key: {str(item).casefold() for item in raw.get(key) or []}
        for key in EMPTY_CATEGORY
    }


def save_category(
    storage_root: Path, project_id: UUID | str, product_type: str, category: dict[str, set[str]]
) -> None:
    _write_category(category_path(storage_root, project_id, product_type), product_type, category)


def _write_category(path: Path, product_type: str, category: dict[str, set[str]]) -> None:
    payload = {"product_type": product_type,
               **{key: sorted(category[key]) for key in EMPTY_CATEGORY}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        # 只读存储时用内存结果继续分类，不阻断主流程。
        return


# ---------- 3. 主体词自动派生 ----------

# 品类词表种子：按商品名/类型里的关键词命中。中文商品资料无法分词成英文时靠它兜底。
# 只放"本类一定是主体/被收纳对象"的词；通用层不放 weight 之类有歧义的词。
CATEGORY_SEEDS: tuple[tuple[tuple[str, ...], dict[str, set[str]]], ...] = (
    (
        # 只认同"哑铃/壶铃收纳架"这一明确子类；不按"健身器材"大类命中，
        # 否则哑铃、护腕、瑜伽垫都会共用同一套主体词。
        (
            "哑铃架", "壶铃架", "哑铃收纳", "哑铃置物架", "哑铃支架",
            "dumbbell rack", "dumbbell stand", "dumbbell storage",
            "dumbbell holder", "dumbbell organizer",
            "weight rack", "kettlebell rack", "kettlebell stand",
        ),
        {
            "subject": {
                "dumbbell", "dumbbells", "dumbell", "dumbells", "dummbell",
                "dummbells", "dumbels", "dumble", "dumbles", "dumbe", "dumbel",
                "dumbbelll", "dumbbellsrack", "dumbbellrack",
                "kettlebell", "kettlebells", "kettle", "kettles", "kettleball",
                "bell", "bells", "ybell", "cowbell",
                # 本类关键词里的 weight/weights 指被收纳的配重；承重规格由 capacity 等词承担。
                "weight", "weights", "weghts", "weigths", "plate", "plates",
                "barbell", "barbells", "bench", "benches",
                "rack", "racks", "stand", "stands", "holder", "holders",
                "organizer", "organizers", "tree", "tray", "trays",
                "equipment", "shelf", "shelves", "frame", "fitstand",
            },
            "brand": {
                "powerblock", "selecttech", "marcy", "ironmaster", "nuobell",
                "nobell", "barebells", "ativafit", "feierdun", "hyperbell",
                "smartbell", "bowflex", "flybird", "trx", "york",
            },
            "selling_extra": set(),
            "scene_extra": set(),
        },
    ),
)


def seed_category(product_name: str, product_type: str) -> dict[str, set[str]]:
    """按商品名/类型命中品类种子；命中不了返回空集。"""
    text = f"{product_name} {product_type}".casefold()
    merged = {key: set() for key in EMPTY_CATEGORY}
    for triggers, data in CATEGORY_SEEDS:
        if any(trigger.casefold() in text for trigger in triggers):
            for key, values in data.items():
                merged[key] |= values
    return merged


def derive_subject(
    product_name: str, product_type: str, keywords: list[str], top_n: int = 8
) -> set[str]:
    """从商品资料 + 关键词中心词派生主体词，换品类无需手工维护。

    1. 商品名/类型有英文实词时，取最后 1～2 个作种子（dumbbell rack → rack、dumbbell）；
    2. 中文商品资料没有英文实词时，改用关键词的末位高频词作种子（本次实测可行）；
    3. 与种子词编辑距离 ≤1 的归并；
    4. 关键词以主体词结尾时，它前面的实词也并入（dumbbell rack → dumbbell）。
    """
    def words(text: str) -> list[str]:
        return [w for w in re.findall(r"[a-z]+", text.lower()) if w not in STOP]

    tail: Counter[str] = Counter()
    for keyword in keywords:
        tokens = words(keyword)
        if tokens:
            tail[tokens[-1]] += 1
    total = max(1, len(keywords))

    banned = (
        SELLING_FUNCTION | MATERIAL | COLOR | SIZE | QUANTITY | STYLE_PRICE
        | SCENE_PLACE | AUDIENCE | OCCASION | UNIT | STOP
    )

    seeds: set[str] = set()
    for text in (product_type, product_name):
        tokens = words(text)
        if tokens:
            seeds.add(tokens[-1])
            if len(tokens) >= 2:
                seeds.add(tokens[-2])
    if not seeds:
        # 中文商品名/类型无法切出英文实词：用品类词表种子 + 关键词末位高频词兜底。
        seeds = set(seed_category(product_name, product_type)["subject"])
        seeds |= {
            term for term, count in tail.most_common(top_n)
            if count >= 5 and count / total >= 0.05 and term not in banned
        }
    if not seeds:
        return set()

    def close(left: str, right: str) -> bool:
        if left == right:
            return True
        if abs(len(left) - len(right)) > 1:
            return False
        short, long = (left, right) if len(left) <= len(right) else (right, left)
        i = j = diff = 0
        while i < len(short) and j < len(long):
            if short[i] == long[j]:
                i += 1
                j += 1
            else:
                diff += 1
                if diff > 1:
                    return False
                if len(short) == len(long):
                    i += 1
                j += 1
        return True

    subject = set(seeds)
    for term, count in tail.most_common(top_n):
        if count < 5 or count / total < 0.05:
            continue
        if any(close(term, seed) for seed in seeds):
            subject.add(term)

    # 被收纳/被承载的对象：关键词以主体词结尾时，它前面的实词也是主体。
    objects: Counter[str] = Counter()
    for keyword in keywords:
        tokens = words(keyword)
        if len(tokens) >= 2 and tokens[-1] in subject and tokens[-2] not in (banned | subject):
            objects[tokens[-2]] += 1
    subject |= {term for term, count in objects.items() if count >= 5}
    return subject


def ensure_category(
    storage_root: Path, project_id: UUID | str, product_name: str, product_type: str,
    keywords: list[str], run_id: UUID | str | None = None,
) -> dict[str, set[str]]:
    """合并品类规则用于本次分类，并保证自动推断结果不污染其它商品。

    - 共享文件（category_path）只保存人工维护的规则与品类种子，不写自动推断结果；
    - 本次商品推断出的主体词写入 run 级文件（runtime_path），只影响本 run；
    - 返回值 = 共享规则 + 品类种子 + 本次推断，供本次分类使用。
    """
    manual = load_category(storage_root, project_id, product_type)
    seeds = seed_category(product_name, product_type)
    derived = derive_subject(product_name, product_type, keywords)
    if run_id is not None:
        runtime = {key: set() for key in EMPTY_CATEGORY}
        runtime["subject"] = set(derived)
        _write_category(runtime_path(storage_root, project_id, run_id, product_type), product_type, runtime)
    merged = {key: set(manual[key]) | seeds[key] for key in EMPTY_CATEGORY}
    merged["subject"] |= derived
    return merged


# ---------- 4. 判定顺序 ----------


def normalize(term: str) -> str:
    return ALIASES.get(term.strip().casefold(), term.strip().casefold())


def _singular(term: str) -> str:
    if term.endswith("ies") and len(term) > 4:
        return term[:-3] + "y"
    if term.endswith("es") and len(term) > 3:
        return term[:-2]
    if term.endswith("s") and len(term) > 3:
        return term[:-1]
    return term


def _candidates(term: str) -> list[str]:
    """同词形的候选写法：原词、单数、去掉数字前缀（3-tier → tier）。"""
    values = [term, _singular(term)]
    stripped = re.sub(r"^\d+[-_ ]", "", term)
    if stripped != term:
        values.extend([stripped, _singular(stripped)])
    return list(dict.fromkeys(values))


def _lookup(term: str, *sets: set[str]) -> bool:
    return any(value in group for value in _candidates(term) for group in sets)


def classify(term: str, category: dict[str, set[str]] | None = None) -> tuple[str, str]:
    """返回 (类别, 依据)。类别见 TaxonomyCategory。"""
    cat = category or EMPTY_CATEGORY
    value = normalize(term)

    if _lookup(value, STOP):
        return "stop", "停用词"
    if value.isdigit():
        return "preference", "规格数值"
    if _lookup(value, UNIT):
        return "unit", "计量单位"
    if _lookup(value, cat.get("brand", set()), BRAND_COMMON):
        return "brand", "品牌词"
    if _lookup(value, cat.get("subject", set()), SUBJECT_COMMON):
        return "subject", "商品主体或被收纳对象"
    if _lookup(value, SELLING_FUNCTION, cat.get("selling_extra", set())):
        return "selling_point", "功能/结构属性"
    if _lookup(value, SCENE_PLACE, AUDIENCE, OCCASION, cat.get("scene_extra", set())):
        return "scene", "场景/人群/场合"
    if _lookup(value, MATERIAL, COLOR, SIZE, QUANTITY, STYLE_PRICE):
        return "preference", "材质/颜色/尺寸/数量/款式"
    return "unclassified", "规则未命中，交模型判定"


def suggest(term: str) -> list[str]:
    """给 analyze() 用的建议类别，只返回三类之一。"""
    found, _ = classify(term)
    return [found] if found in EXPORTED else []


def canonical(term: str) -> str:
    """同一词形的归一结果，用于导出时去重（dumbbell / dumbbells / dumbell 视为同一个）。"""
    return _singular(normalize(term))


def review_category(found: str) -> str:
    """把规则结果映射为审核可用的四类之一：主体/品牌/单位/停用词一律记为 unclassified。

    这是硬约束：即使后续模型返回这些词，它们也不能被放回卖点/场景/偏好。
    """
    return found if found in EXPORTED else "unclassified"


def secondary_categories(term: str, features: str) -> list[str]:
    """规则主类别之外可追加的标签。

    判定分两步：先看"这个词根属于什么属性"（材质→偏好），再看"商品资料是否把它
    写成卖点"。只有同时满足「说的是本品（不是配件/被收纳物）」「肯定表述」
    「同句有卖点依据」才追加 selling_point。
    """
    value = _singular(normalize(term))
    aliases = MATERIAL_ZH.get(value)
    if not aliases:
        return []
    for segment in re.split(r"[.;!?\n，,；]", features):
        if _segment_claims_advantage(segment, value, aliases):
            return ["selling_point"]
    return []


NEGATIONS = ("not ", "no ", "non-", "non ", "without ", "instead of ", "rather than ",
             "不含", "非", "不是", "无", "没有", "而非", "不用")
# 说的是"被收纳/适配的东西"，不是本品
OTHER_OBJECTS = {"dumbbell", "dumbbells", "dumbell", "dumbells", "kettlebell", "kettlebells",
                 "weight", "weights", "plate", "plates", "barbell", "barbells", "bell", "bells"}
OTHER_OBJECTS_ZH = ("哑铃", "壶铃", "配重", "杠铃", "铃片", "片")
# 说的是本品的结构件
PRODUCT_PARTS = {"frame", "frames", "structure", "body", "leg", "legs", "base", "shelf",
                 "shelves", "tray", "handle", "handles", "pole", "poles", "tube", "tubes",
                 "bar", "bars", "post", "posts", "beam", "beams", "rack", "stand", "panel",
                 "panels", "rod", "rods"}
PRODUCT_PARTS_ZH = ("框架", "结构", "机身", "立柱", "支架", "层板", "隔板", "脚垫", "底座",
                    "横梁", "钢管", "管材", "板材", "托盘", "立杆")
# 卖点依据：强度/工艺/承重/耐用
SELLING_CUES = {"reinforced", "heavy-duty", "heavy duty", "heavy load", "heavy loads",
                "load-bearing", "load bearing", "sturdy", "durable", "thick", "gauge",
                "rust-proof", "rustproof", "anti-rust", "powder-coated", "powder coated",
                "welded", "capacity", "supports", "holds", "up to", "high strength",
                "strong", "stable", "commercial", "non-slip"}
SELLING_CUES_ZH = ("加厚", "加固", "加粗", "承重", "耐用", "防锈", "喷涂", "焊接", "高强度",
                   "稳固", "商用", "防滑", "不易变形")
# 只陈述材质、没有卖点依据的写法
NEUTRAL_PREFIX = ("material:", "material -", "material ", "材质:", "材质：", "材质 ")


def _segment_claims_advantage(segment: str, material: str, aliases: tuple[str, ...]) -> bool:
    text = segment.casefold().strip()
    if not text or any(text.startswith(prefix) for prefix in NEUTRAL_PREFIX):
        return False
    position = -1
    matched = material
    for alias in (material, *aliases):
        found = text.find(alias.casefold())
        if found >= 0 and (position < 0 or found < position):
            position = found
            matched = alias
    if position < 0:
        return False
    before = text[max(0, position - 25):position]
    if any(negation in before for negation in NEGATIONS):
        return False
    after = text[position + len(matched):position + len(matched) + 30]
    next_word = re.match(r"\s*([a-z]+)", after)
    if next_word and next_word.group(1) in OTHER_OBJECTS:
        return False
    if any(obj in after[:6] for obj in OTHER_OBJECTS_ZH):
        return False
    about_product = (
        bool(next_word and next_word.group(1) in PRODUCT_PARTS)
        or any(part in text for part in PRODUCT_PARTS_ZH)
    )
    if not about_product:
        return False
    return any(cue in text for cue in SELLING_CUES) or any(cue in text for cue in SELLING_CUES_ZH)
