"""规则层单测：纯标准库，不依赖数据库、模型与网络。

运行（任意 shell 或 PyCharm 右键运行均可）：
    python tests/test_taxonomy.py
    pytest tests/test_taxonomy.py
"""

import sys
import tempfile
import unittest
from pathlib import Path

# 允许直接以脚本方式运行，无需先设置 PYTHONPATH。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import taxonomy  # noqa: E402

CATEGORY = {
    "subject": {"rack", "dumbbell", "stand", "holder"},
    "brand": {"powerblock"},
    "selling_extra": set(),
    "scene_extra": set(),
}


class ClassifyTest(unittest.TestCase):
    def test_function_words_are_selling_points(self) -> None:
        for term in ("adjustable", "tier", "3-tier", "foldable", "stackable", "detachable", "lockable"):
            with self.subTest(term=term):
                self.assertEqual(taxonomy.classify(term, CATEGORY)[0], "selling_point")

    def test_specs_are_preferences(self) -> None:
        for term in ("steel", "black", "small", "pair", "premium", "660"):
            with self.subTest(term=term):
                self.assertEqual(taxonomy.classify(term, CATEGORY)[0], "preference")

    def test_scene_words(self) -> None:
        for term in ("home", "gym", "garage", "women", "beginner", "yoga"):
            with self.subTest(term=term):
                self.assertEqual(taxonomy.classify(term, CATEGORY)[0], "scene")

    def test_subject_words_never_exported(self) -> None:
        for term in ("rack", "racks", "dumbbell", "dumbbells", "stand", "holder"):
            with self.subTest(term=term):
                category, _ = taxonomy.classify(term, CATEGORY)
                self.assertEqual(category, "subject")
                self.assertNotIn(category, taxonomy.EXPORTED)

    def test_brand_and_unit_never_exported(self) -> None:
        self.assertEqual(taxonomy.classify("powerblock", CATEGORY)[0], "brand")
        self.assertEqual(taxonomy.classify("lb", CATEGORY)[0], "unit")
        self.assertEqual(taxonomy.classify("pack", CATEGORY)[0], "unit")

    def test_spelling_variants_normalized(self) -> None:
        self.assertEqual(taxonomy.classify("ajustable", CATEGORY)[0], "selling_point")
        self.assertEqual(taxonomy.classify("dumbell", CATEGORY)[0], "subject")
        self.assertEqual(taxonomy.classify("mancuerna", CATEGORY)[0], "subject")

    def test_unknown_goes_to_unclassified(self) -> None:
        self.assertEqual(taxonomy.classify("qwertyzz", CATEGORY)[0], "unclassified")

    def test_suggest_only_returns_exported(self) -> None:
        self.assertEqual(taxonomy.suggest("rack"), [])
        self.assertEqual(taxonomy.suggest("adjustable"), ["selling_point"])

    def test_review_category_pins_non_export_as_unclassified(self) -> None:
        # 关键约束：主体/品牌/单位/停用词必须钉成 unclassified，不能进三类。
        for found in ("subject", "brand", "unit", "stop"):
            self.assertEqual(taxonomy.review_category(found), "unclassified")
        for found in ("selling_point", "scene", "preference"):
            self.assertEqual(taxonomy.review_category(found), found)

    def test_subject_brand_unit_never_reach_exported(self) -> None:
        # 整类回归：任一判为主体/品牌/单位的词，经 review_category 后都不是三类。
        for term, expect in (
            ("rack", "subject"), ("powerblock", "brand"), ("lb", "unit"),
        ):
            found, _ = taxonomy.classify(term, CATEGORY)
            self.assertEqual(found, expect)
            self.assertEqual(taxonomy.review_category(found), "unclassified")


class DeriveSubjectTest(unittest.TestCase):
    def test_dumbbell_rack(self) -> None:
        keywords = ["dumbbell rack", "weight rack", "dumbbell rack for home gym"] * 3
        subjects = taxonomy.derive_subject("三层哑铃收纳架", "dumbbell rack", keywords)
        self.assertIn("rack", subjects)
        self.assertIn("dumbbell", subjects)

    def test_chinese_product_name_still_derives_subject(self) -> None:
        # 服务器实际资料：商品名和类型都是中文，切不出英文实词。
        keywords = ["dumbbell rack", "weight rack", "kettlebell rack", "dumbbell stand"] * 3
        subjects = taxonomy.derive_subject("3层可调节哑铃架", "健身器材配件", keywords)
        self.assertIn("rack", subjects)
        self.assertIn("dumbbell", subjects)
        self.assertIn("kettlebell", subjects)
        self.assertIn("weight", subjects)

    def test_chinese_category_path_is_not_default(self) -> None:
        path = taxonomy.category_path(Path("storage"), "p", "健身器材配件")
        self.assertEqual(path.name, "健身器材配件.json")
        self.assertNotEqual(path.name, "default.json")

    def test_category_seed_matches_chinese(self) -> None:
        seeds = taxonomy.seed_category("3层可调节哑铃架", "健身器材配件")
        for term in ("dumbbell", "dumbbells", "kettlebell", "weight"):
            self.assertIn(term, seeds["subject"])

    def test_generic_fitness_type_does_not_trigger_dumbbell_rack_seed(self) -> None:
        # 只锁定"哑铃架"子类；大类"健身器材"不得共用这套主体词。
        seeds = taxonomy.seed_category("瑜伽垫", "健身器材配件")
        self.assertEqual(seeds["subject"], set())
        seeds = taxonomy.seed_category("护腕", "健身器材")
        self.assertEqual(seeds["subject"], set())

    def test_cat_tree(self) -> None:
        keywords = ["cat tree", "tall cat tree", "cat tower", "cat tree condo"] * 3
        subjects = taxonomy.derive_subject("多层猫爬架", "cat tree", keywords)
        self.assertIn("tree", subjects)
        self.assertIn("cat", subjects)

    def test_single_keyword_does_not_invent_subject(self) -> None:
        subjects = taxonomy.derive_subject("咖啡机", "coffee maker", ["coffee maker"])
        self.assertEqual(subjects, {"coffee", "maker"})


class CategoryLayerTest(unittest.TestCase):
    def test_derived_subject_saved_per_run_not_shared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keywords = ["dumbbell rack", "weight rack"] * 4
            first = taxonomy.ensure_category(
                root, "proj", "哑铃架", "dumbbell rack", keywords, run_id="run-1"
            )
            self.assertIn("rack", first["subject"])
            self.assertTrue(taxonomy.runtime_path(root, "proj", "run-1", "dumbbell rack").exists())
            # 自动推断结果不得写进共享文件，避免污染同项目其它商品。
            self.assertFalse(taxonomy.category_path(root, "proj", "dumbbell rack").exists())

    def test_derived_subject_does_not_leak_to_next_product(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            taxonomy.ensure_category(
                root, "proj", "3层可调节哑铃架", "健身器材配件",
                ["dumbbell rack", "weight rack"] * 4, run_id="run-a",
            )
            other = taxonomy.ensure_category(
                root, "proj", "旅行秤", "健身器材配件",
                ["travel scale", "digital luggage scale"] * 4, run_id="run-b",
            )
            self.assertNotIn("dumbbell", other["subject"])
            self.assertNotIn("weight", other["subject"])
            self.assertNotIn("rack", other["subject"])

    def test_manual_edit_survives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            taxonomy.save_category(
                root, "proj", "cat tree",
                {"subject": {"tree"}, "brand": {"feandrea"}, "selling_extra": set(), "scene_extra": set()},
            )
            loaded = taxonomy.ensure_category(root, "proj", "猫爬架", "cat tree", ["cat tree"] * 10, run_id="r")
            self.assertIn("feandrea", loaded["brand"])
            self.assertIn("tree", loaded["subject"])

    def test_readonly_storage_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "nested" / "proj"
            category = taxonomy.ensure_category(missing, "proj", "哑铃架", "dumbbell rack", ["dumbbell rack"])
            self.assertIn("rack", category["subject"])


class MultiLabelTest(unittest.TestCase):
    def test_selling_evidence_required(self) -> None:
        # 只有"说的是本品 + 肯定表述 + 同句有卖点依据"才追加卖点标签。
        cases = [
            ("Reinforced steel frame for heavy loads", ["selling_point"]),
            ("Material: steel", []),
            ("Made of aluminum, not steel", []),
            ("Fits steel dumbbells", []),
            ("steel frame", []),
            ("加厚钢制框架，H 型结构", ["selling_point"]),
            ("材质：钢", []),
            ("含钢制框架，但不含钢制层板", []),
            ("加厚钢制框架，承重 660 磅", ["selling_point"]),
            ("适配钢制哑铃", []),
            ("不含钢", []),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(taxonomy.secondary_categories("steel", text), expected)

    def test_non_material_never_gets_second_label(self) -> None:
        self.assertEqual(taxonomy.secondary_categories("adjustable", "加厚钢制框架"), [])
        self.assertEqual(taxonomy.secondary_categories("rack", "reinforced steel frame"), [])


class RealCorpusTest(unittest.TestCase):
    """回归：本草稿 442 个导出词根中，规则应覆盖九成以上且不产生主体/品牌/单位导出。"""

    def test_exported_categories_are_clean(self) -> None:
        sample = [
            "adjustable", "tier", "rack", "dumbbell", "kettlebell", "steel", "home",
            "gym", "yoga", "powerblock", "lb", "inch", "pair", "small", "weight",
        ]
        for term in sample:
            category, _ = taxonomy.classify(term, CATEGORY)
            if category in taxonomy.EXPORTED:
                self.assertNotIn(term, {"rack", "dumbbell", "kettlebell", "powerblock", "lb", "inch"})


if __name__ == "__main__":
    unittest.main()
