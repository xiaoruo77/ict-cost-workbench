"""Deterministic regression tests; no model calls or credential access.

Run from the complete project: python -B -m unittest discover -s tests -p test_cost_conventions.py -v
"""
from __future__ import annotations

import copy
import csv
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cost_conventions as CC
import report_facts as RF
import benchmark_engine as BE
import period_agg as PA


SELF = "csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv"
PEER = "csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv"
MARKET = "csv/market_data/药材市场价格行情_2026年上半年.csv"
MATERIAL = "csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv"
COLUMNS = (("单位成本", "单位成本(元/盒)"), ("直接材料", "直接材料(元/盒)"),
           ("直接人工", "直接人工(元/盒)"), ("制造费用", "制造费用(元/盒)"))


def rounded(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fixture_tables():
    tables = {SELF: [], PEER: [], MATERIAL: [], MARKET: [{
        "药材名称": "金银花", "单位": "元/公斤", "1月价格": "10", "2月价格": "12",
        "3月价格": "15", "4月价格": "20", "5月价格": "25", "6月价格": "30",
        "趋势分析": "全年叙述不可用作早期已知原因",
    }]}
    for pi, product in enumerate(RF.UNIT_PER_BOX):
        for month in range(1, 7):
            values = [Decimal("6.10") + pi + Decimal(month) / 10,
                      Decimal("2.20") + Decimal(month) / 20,
                      Decimal("1.30") + Decimal(pi) / 10]
            for peer, path in ((False, SELF), (True, PEER)):
                elems = [v + (Decimal("0.17") if (pi + month + i) % 2 else Decimal("-0.09"))
                         if peer else v for i, v in enumerate(values)]
                # Deliberately different factory output weights detect period mismatch.
                qty = (7 - month) * 125 if peer else month * 100
                unit = sum(elems)
                row = {"产品名称": product, "月份": f"2026-{month:02d}",
                       "产量(盒)": str(qty), "总成本(元)": str(unit * qty),
                       "单位成本(元/盒)": str(unit)}
                row.update({col: str(v) for (_, col), v in zip(COLUMNS[1:], elems)})
                tables[path].append(row)
            tables[MATERIAL].append({"产品名称": product, "月份": f"2026-{month:02d}",
                "原材料名称": "金银花", "单位消耗成本(元/盒)": "1.25",
                "原材料总成本(元)": str(Decimal("1.25") * month * 100),
                "产量(盒)": str(month * 100), "占总材料成本比例": "20%"})
    return tables


class CostRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tables = fixture_tables()
        self.original_override = RF._OVERRIDE
        RF.use_overrides(None)
        self.addCleanup(RF.use_overrides, self.original_override)

        def read(name):
            override = RF._OVERRIDE
            if override is not None and name in override:
                return copy.deepcopy(override[name])
            return copy.deepcopy(self.tables.get(name, []))

        self.addCleanup(patch.stopall)
        patch.object(RF, "_read", side_effect=read).start()
        patch.object(RF, "_data_conflicts", return_value=[]).start()
        patch.object(BE, "_read", side_effect=read).start()

    def test_decimal_rounding_missing_and_zero_base(self):
        self.assertEqual(CC.format_number("2.675"), "2.68")
        self.assertEqual(CC.format_number("-2.675"), "-2.68")
        self.assertEqual(CC.format_number(None), "—")
        for invalid in (None, "", "—", "NaN", "Infinity"):
            self.assertIsNone(CC.as_decimal(invalid))
        result = CC.compare_cost("0", "2")
        self.assertEqual(result["difference"], 2)
        self.assertIsNone(result["rate"])
        self.assertEqual(result["direction"], "二厂高")
        self.assertIsNone(CC.compare_cost(None, "2")["difference"])

    def test_all_18_product_months_times_four_dimensions(self):
        checked = 0
        for own in self.tables[SELF]:
            product, month = own["产品名称"], own["月份"]
            peer = next(r for r in self.tables[PEER]
                        if r["产品名称"] == product and r["月份"] == month)
            facts = RF.build(product, month)
            engine = BE.step1_find_diff(product, month)
            for label, col in COLUMNS:
                with self.subTest(product=product, month=month, element=label):
                    a, b = Decimal(own[col]), Decimal(peer[col])
                    diff, rate = b - a, (b - a) / a * 100
                    f = next(r for r in facts.peer if r["维度"] == label)
                    e = next(r for r in engine["rows"] if r["成本要素"] == label)
                    self.assertEqual(rounded(f["差异金额"]), rounded(diff))
                    self.assertEqual(Decimal(f["差异率"].rstrip("%")), rounded(rate))
                    self.assertEqual(rounded(e["差异金额"]), rounded(diff))
                    self.assertEqual(rounded(e["差异率"]), rounded(rate))
                    self.assertEqual(f["方向"], "二厂高" if diff > 0 else "一厂高" if diff < 0 else "持平")
                    checked += 1
        self.assertEqual(checked, 72)

    def test_quarter_uses_each_factory_own_quantity_weights(self):
        product = next(iter(RF.UNIT_PER_BOX))
        facts, meta = PA.build_quarter(product, "2026-Q1")
        expected = {}
        for path in (SELF, PEER):
            rows = [r for r in self.tables[path]
                    if r["产品名称"] == product and r["月份"] <= "2026-03"]
            expected[path] = (sum(Decimal(r["总成本(元)"]) for r in rows)
                              / sum(Decimal(r["产量(盒)"]) for r in rows))
        row = next(r for r in facts.peer if r["维度"] == "单位成本")
        self.assertEqual(Decimal(row["中药一厂"]), rounded(expected[SELF]))
        self.assertEqual(Decimal(row["中药二厂"]), rounded(expected[PEER]))
        self.assertEqual(rounded(row["差异金额"]), rounded(expected[PEER] - expected[SELF]))
        self.assertEqual(meta["missing_months"], [])
        self.assertEqual([row["月份"] for row in facts.monthly],
                         ["2026-01", "2026-02", "2026-03"])
        self.assertIsNone(RF._OVERRIDE, "Quarter overrides must be cleared after use")

    def test_analysis_cutoff_market_month_and_material_source(self):
        facts = RF.build(next(iter(RF.UNIT_PER_BOX)), "2026-03")
        self.assertEqual([r["月份"] for r in facts.monthly],
                         ["2026-01", "2026-02", "2026-03"])
        market = next(r for r in facts.market if r["原材料"] == "金银花")
        self.assertEqual(Decimal(str(market["本期价"])), Decimal("15"))
        self.assertEqual(market["价格月份"], "2026-03")
        self.assertEqual(Decimal(market["涨幅"].rstrip("%")), Decimal("50"))
        self.assertTrue(facts.materials)
        for material in facts.materials:
            self.assertNotEqual(material["单价来源"], "中药材市场")
            self.assertIn("明细", material["单价来源"])

    def test_zero_base_and_missing_comparison_do_not_crash_brief(self):
        product = next(iter(RF.UNIT_PER_BOX))
        for row in self.tables[SELF]:
            if row["产品名称"] == product and row["月份"] == "2026-03":
                for _, column in COLUMNS:
                    row[column] = "0"
        with patch.object(BE, "context_for", return_value="无匹配事件，需进一步核查。"):
            result = BE.run(product, "2026-03", use_llm=False, verbose=False)
        self.assertTrue(all(row["差异率"] is None for row in result["steps"][0]["rows"]))
        self.assertIn("不可计算（一厂为0）", result["steps"][2]["brief"])
        self.assertNotIn("产地减产", result["steps"][2]["brief"])
        self.tables[PEER] = []
        missing = BE.run(product, "2026-03", use_llm=False, verbose=False)
        self.assertIn("缺少", missing["steps"][2]["text"])


class ActualDatasetRegressionTests(unittest.TestCase):
    @unittest.skipUnless((RF.RAW / SELF).is_file() and (RF.RAW / PEER).is_file(),
                         "Actual contest CSV data are not installed in this checkout")
    def test_actual_18_product_months_times_four_dimensions(self):
        def read(path):
            with (RF.RAW / path).open(encoding="utf-8-sig", newline="") as stream:
                return list(csv.DictReader(stream))
        own_rows, peer_rows = read(SELF), read(PEER)
        pairs = {(row["产品名称"], row["月份"]) for row in own_rows}
        self.assertEqual(len(pairs), 18)
        checked = 0
        for product, month in sorted(pairs):
            own = next(row for row in own_rows if (row["产品名称"], row["月份"]) == (product, month))
            peer = next(row for row in peer_rows if (row["产品名称"], row["月份"]) == (product, month))
            facts, engine = RF.build(product, month), BE.step1_find_diff(product, month)
            for label, column in COLUMNS:
                with self.subTest(product=product, month=month, element=label):
                    a, b = Decimal(own[column].replace(",", "")), Decimal(peer[column].replace(",", ""))
                    expected_diff = rounded(b - a)
                    expected_rate = rounded((b - a) / a * 100) if a else None
                    fact = next(row for row in facts.peer if row["维度"] == label)
                    bench = next(row for row in engine["rows"] if row["成本要素"] == label)
                    self.assertEqual(rounded(fact["差异金额"]), expected_diff)
                    self.assertEqual(rounded(bench["差异金额"]), expected_diff)
                    self.assertEqual(rounded(bench["差异率"]) if bench["差异率"] is not None else None,
                                     expected_rate)
                    self.assertEqual(Decimal(fact["差异率"].rstrip("%")) if fact["差异率"] != "—" else None,
                                     expected_rate)
                    checked += 1
        self.assertEqual(checked, 72)


if __name__ == "__main__":
    unittest.main()
