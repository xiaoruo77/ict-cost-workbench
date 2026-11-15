"""Regression tests for Markdown table boundaries in the DOCX export."""
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from export_doc import to_docx


W = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

REQUIRED_HEADER = ["对象", "成本项", "资料状态", "金额"]
REQUIRED_ROW = ["store-1-dine-food-rice", "utilities", "已提供", "1.60"]
PARTITION_HEADER = ["对象", "在制", "完工库存", "净销售成本", "报废损失", "合格单位成本"]
PARTITION_ROW = ["store-1-dine-food-rice", "0.00", "12.00", "48.00", "6.00", "6.00"]

REQUIRED_TABLE = (
    "| 对象 | 成本项 | 资料状态 | 金额 |\n"
    "| :--- | --- | :---: | ---: |\n"
    "| store-1-dine-food-rice | utilities | 已提供 | 1.60 |\n"
)
PARTITION_TABLE = (
    "| 对象 | 在制 | 完工库存 | 净销售成本 | 报废损失 | 合格单位成本 |\n"
    "| :--- | ---: | ---: | ---: | ---: | ---: |\n"
    "| store-1-dine-food-rice | 0.00 | 12.00 | 48.00 | 6.00 | 6.00 |\n"
)


class DocxTableBoundaryTests(unittest.TestCase):
    def render_tables(self, markdown):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "report.md"
            source.write_text(markdown, encoding="utf-8")
            target = to_docx(source, root / "report.docx")
            with ZipFile(target) as docx:
                document = ET.fromstring(docx.read("word/document.xml"))
        return document.findall("./w:body/w:tbl", W)

    def cells(self, row):
        return [
            "".join(t.text or "" for t in cell.findall(".//w:t", W))
            for cell in row.findall("./w:tc", W)
        ]

    def assert_table(self, table, header, body):
        width = len(header)
        self.assertEqual(len(table.findall("./w:tblGrid/w:gridCol", W)), width)
        rows = table.findall("./w:tr", W)
        self.assertEqual([self.cells(row) for row in rows], [header, *body])
        self.assertEqual(
            [i for i, row in enumerate(rows) if row.find("./w:trPr/w:tblHeader", W) is not None],
            [0],
        )

    def assert_required_and_partition_tables(self, markdown):
        tables = self.render_tables(markdown)
        self.assertEqual(
            len(tables), 2,
            "The required-cost and cost-partition blocks must be distinct Word tables",
        )
        self.assert_table(tables[0], REQUIRED_HEADER, [REQUIRED_ROW])
        self.assert_table(tables[1], PARTITION_HEADER, [PARTITION_ROW])

    def test_single_table_retains_its_grid_and_repeat_header(self):
        tables = self.render_tables(REQUIRED_TABLE)
        self.assertEqual(len(tables), 1)
        self.assert_table(tables[0], REQUIRED_HEADER, [REQUIRED_ROW])

    def test_adjacent_tables_with_blank_line_stay_independent(self):
        self.assert_required_and_partition_tables(REQUIRED_TABLE + "\n" + PARTITION_TABLE)

    def test_adjacent_tables_without_blank_line_stay_independent(self):
        self.assert_required_and_partition_tables(REQUIRED_TABLE + PARTITION_TABLE)

    def test_adjacent_tables_of_the_same_width_stay_independent(self):
        markdown = REQUIRED_TABLE + (
            "| 费用对象 | 科目 | 校验状态 | 期末值 |\n"
            "| --- | --- | --- | ---: |\n"
            "| store-2 | release | 已提供 | 40.00 |\n"
        )
        tables = self.render_tables(markdown)
        self.assertEqual(len(tables), 2)
        self.assert_table(tables[0], REQUIRED_HEADER, [REQUIRED_ROW])
        self.assert_table(
            tables[1], ["费用对象", "科目", "校验状态", "期末值"],
            [["store-2", "release", "已提供", "40.00"]],
        )

    def test_mismatched_or_escaped_separator_is_not_a_new_header(self):
        for separator in ("| --- | --- |", r"| --- \| --- | --- | --- | --- |"):
            with self.subTest(separator=separator):
                markdown = REQUIRED_TABLE + (
                    "| store-2 | packaging | 已提供 | 13.00 |\n"
                    f"{separator}\n"
                    "| store-3 | materials | 已提供 | 2.00 |\n"
                )
                tables = self.render_tables(markdown)
                self.assertEqual(len(tables), 1)
                rows = tables[0].findall("./w:tr", W)
                self.assertEqual(self.cells(rows[0]), REQUIRED_HEADER)
                self.assertIn(["store-2", "packaging", "已提供", "13.00"], [self.cells(r) for r in rows])
                self.assertEqual(
                    [i for i, row in enumerate(rows) if row.find("./w:trPr/w:tblHeader", W) is not None],
                    [0],
                )

    def test_escaped_pipe_is_literal_in_header_and_data(self):
        markdown = (
            r"| 来源 \| 凭证 | 金额 |" "\n"
            "| --- | ---: |\n"
            r"| FIN \| 原始行 | 13.00 |" "\n"
        )
        tables = self.render_tables(markdown)
        self.assertEqual(len(tables), 1)
        self.assert_table(tables[0], ["来源 | 凭证", "金额"], [["FIN | 原始行", "13.00"]])


if __name__ == "__main__":
    unittest.main()
