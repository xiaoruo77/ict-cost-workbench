"""Prove the legacy period entry executes neutral weighting and preserves goldens."""
from pathlib import Path
import csv
import json
import os
import sys
import unittest
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import period_agg as PA
import report_facts as RF
from industry_plugins.pharma import periods
from cost_core import aggregation


def rows():
    return [{"工厂": "中药一厂", "产品名称": "银黄口服液", "产品规格": "10ml×10支/盒",
             "月份": month, "产量(盒)": quantity, "总成本(元)": total,
             "单位成本(元/盒)": "99", "直接材料(元/盒)": "0",
             "直接人工(元/盒)": "1", "制造费用(元/盒)": "2"}
            for month, quantity, total in (("2026-01", "1", "10"), ("2026-02", "3", "30"))]


class PeriodFacadeTests(unittest.TestCase):
    def test_alias_identity_and_core_weighting_are_used(self):
        self.assertIs(PA, periods)
        with patch.object(aggregation, "weighted_observations", wraps=aggregation.weighted_observations) as spy:
            result = PA.aggregate(rows(), "银黄口服液", ["2026-01", "2026-02"])
            synthetic = PA._synth_row(rows(), "银黄口服液", ["2026-01", "2026-02"], "2026-02")
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(result["产量"], 4)
        self.assertEqual(result["单位成本"], 10, "Legacy plugin explicitly derives reported_total/quantity")
        self.assertEqual(result["要素单位"]["直接材料"], 0)
        self.assertEqual(synthetic["直接材料(元/盒)"], "0")
        self.assertEqual(synthetic["__input_periods"], ("2026-01", "2026-02"))
        self.assertTrue(synthetic["__core_sources"]["unit_cost"][-1].formula)

    def test_missing_is_not_zero_and_missing_month_is_not_complete(self):
        fixture = rows()
        fixture[0]["总成本(元)"] = ""
        fixture[0]["直接材料(元/盒)"] = ""
        result = PA.aggregate(fixture, "银黄口服液", ["2026-01", "2026-02"])
        self.assertIsNone(result["总成本"])
        self.assertIsNone(result["单位成本"])
        self.assertIsNone(result["要素单位"]["直接材料"])
        missing = PA.aggregate(fixture[:1], "银黄口服液", ["2026-01", "2026-02"])
        self.assertFalse(missing["完整"])
        self.assertEqual(missing["缺月"], ["2026-02"])
        self.assertIsNone(PA._synth_row(fixture[:1], "银黄口服液", ["2026-01", "2026-02"], "2026-02"))

    def test_real_zero_output_retains_observed_total(self):
        fixture = rows()
        for row in fixture:
            row["产量(盒)"] = "0"
            row["总成本(元)"] = "7"
        result = PA.aggregate(fixture, "银黄口服液", ["2026-01", "2026-02"])
        self.assertEqual(result["产量"], 0)
        self.assertEqual(result["总成本"], 14)
        self.assertIsNone(result["单位成本"])


class RealPeriodGoldenTests(unittest.TestCase):
    def test_six_quarters_preserve_valid_goldens_and_recompute_defective_denominators(self):
        staging = ROOT.parent
        raw = ROOT / "raw" if (ROOT / "raw").is_dir() else staging / "data" / "raw"
        golden = Path(os.environ.get("COST_TEST_BASELINE", str(staging / "stage2_goldens.json")))
        if not raw.is_dir() or not golden.is_file():
            self.skipTest("Full source data and stage-two golden fixture are unavailable")
        fixtures = json.loads(golden.read_text(encoding="utf-8"))["quarterly"]
        self.assertEqual(len(fixtures), 6)
        original_read = RF._read

        def read(filename):
            if RF._OVERRIDE is not None and filename in RF._OVERRIDE:
                return RF._OVERRIDE[filename]
            with (raw / filename).open(encoding="utf-8-sig", newline="") as stream:
                return list(csv.DictReader(stream))

        with patch.object(RF, "_read", side_effect=read):
            for fixture in fixtures:
                with self.subTest(product=fixture["product"], quarter=fixture["quarter"]):
                    facts, meta = PA.build_quarter(fixture["product"], fixture["quarter"])
                    for name, expected in fixture["facts"]["facts"].items():
                        self.assertIn(name, facts.facts)
                        value = facts.facts[name].value
                        categories = ("折旧费", "动力费(水电气)", "间接人工", "检验费", "其他制造费用")
                        category = next((item for item in categories if name in ("本月" + item, "上月" + item)), None)
                        if category and expected["value"] is not None:
                            # The historical golden repeated the production denominator
                            # once per expense category. Preserve that file for audit;
                            # derive this correction independently from the raw CSV.
                            year, quarter = fixture["quarter"].split("-Q")
                            q = int(quarter) - (1 if name.startswith("上月") else 0)
                            self.assertGreater(q, 0)
                            months = {f"{year}-{month:02d}" for month in range((q - 1) * 3 + 1, q * 3 + 1)}
                            source_category = "人工(间接)" if category == "间接人工" else category
                            with (raw / "csv/cost_data/中药一厂_制造费用明细_2026年1-6月.csv").open(encoding="utf-8-sig", newline="") as stream:
                                detail = [row for row in csv.DictReader(stream) if row["产品名称"] == fixture["product"]
                                          and row["月份"] in months and row["费用类别"] == source_category]
                            self.assertEqual({row["月份"] for row in detail}, months)
                            denominator = sum((Decimal(row["产量(盒)"]) for row in detail), Decimal(0))
                            independent = sum((Decimal(row["单位费用(元/盒)"]) * Decimal(row["产量(盒)"])
                                               for row in detail), Decimal(0)) / denominator
                            self.assertAlmostEqual(value, float(independent), delta=1e-10, msg=name)
                            self.assertEqual(facts.facts[name].display,
                                             format(independent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"))
                            continue
                        if expected["value"] is None:
                            self.assertIsNone(value)
                        else:
                            self.assertAlmostEqual(value, expected["value"], delta=1e-7, msg=name)
                        self.assertEqual(facts.facts[name].display, expected["display"], name)
                    self.assertEqual(meta["missing_months"], [])
                    self.assertIsNone(RF._OVERRIDE)


if __name__ == "__main__":
    unittest.main()
