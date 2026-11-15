"""Regressions for actual numeric Matplotlib axes, without writing chart images."""
from pathlib import Path
import math
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chart_render as CR


class ChartNumericTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.plt = plt
        self.addCleanup(plt.close, "all")
        patcher = patch.object(CR, "_setup_font", return_value="test-font")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.figures = []
        def retain_figure(figure, out):
            self.figures.append(figure)
            return out
        patcher = patch.object(CR, "_save", side_effect=retain_figure)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rf_display_strings_create_numeric_axis_and_real_march_decline(self):
        rows = [
            {"月份": "2026-01", "单位成本": "10.70", "单位材料": "6.83", "单位人工": "1.52", "单位制造费用": "2.35"},
            {"月份": "2026-02", "单位成本": "10.87", "单位材料": "6.92", "单位人工": "1.55", "单位制造费用": "2.40"},
            {"月份": "2026-03", "单位成本": "10.53", "单位材料": "6.77", "单位人工": "1.48", "单位制造费用": "2.28"},
        ]
        CR.render_trend(rows, "银黄口服液", Path("unused.png"))
        ax = self.figures[0].axes[0]
        self.assertEqual(len(ax.lines), 4)
        for line, column in zip(ax.lines, ("单位成本", "单位材料", "单位人工", "单位制造费用")):
            original = list(line.get_ydata(orig=True))
            self.assertTrue(all(isinstance(value, (int, float)) for value in original))
            self.assertEqual(original, [float(row[column]) for row in rows])
            plotted = list(line.get_ydata(orig=False))
            self.assertLess(plotted[2], plotted[1], "March must decline rather than receive the next category index")
        self.assertNotEqual(type(ax.yaxis.get_converter()).__name__ if hasattr(ax.yaxis, "get_converter")
                            else type(ax.yaxis.converter).__name__, "StrCategoryConverter")

    def test_missing_trend_value_is_gap_not_zero_and_other_series_unchanged(self):
        rows = [{"月份": "2026-01", "单位成本": "10.70"},
                {"月份": "2026-02", "单位成本": "—"},
                {"月份": "2026-03", "单位成本": "10.53"}]
        CR.render_trend(rows, "银黄口服液", Path("unused.png"))
        ax = self.figures[0].axes[0]
        self.assertEqual(len(ax.lines), 1)
        self.assertTrue(math.isnan(ax.lines[0].get_ydata()[1]))
        self.assertEqual(ax.lines[0].get_ydata()[2], 10.53)

    def test_peer_strings_are_numeric_heights_and_missing_is_not_zero(self):
        CR.render_peer([{"成本要素": "直接材料", "一厂": "7.30", "二厂": "7.29"},
                        {"成本要素": "直接人工", "一厂": "—", "二厂": "1.73"}],
                       "银黄口服液", Path("unused.png"))
        ax = self.figures[0].axes[0]
        self.assertEqual(ax.containers[0].patches[0].get_height(), 7.30)
        self.assertTrue(math.isnan(ax.containers[0].patches[1].get_height()))
        self.assertEqual(ax.containers[1].patches[1].get_height(), 1.73)


if __name__ == "__main__":
    unittest.main()
