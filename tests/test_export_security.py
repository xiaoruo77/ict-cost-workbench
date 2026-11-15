"""Export regressions: input markup never gains generated cover/chart privileges."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_doc as E
import costing_outputs as CO

ROOT = Path(__file__).resolve().parents[1]


def png(path):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 10), "#0f766e").save(path)
    return path


def nodes(value):
    if isinstance(value, dict):
        if "t" in value:
            yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def document():
    return {"pandoc-api-version": [1, 23, 1],
            "meta": {"header-includes": {"t": "MetaBlocks", "c": [{"t": "RawBlock", "c": ["latex", r"\input{/private/secret}"]}]}},
            "blocks": [
                {"t": "Header", "c": [2, ["anchor", [], []], [{"t": "Str", "c": "Generated"}]]},
                {"t": "Para", "c": [{"t": "Image", "c": [["", [], []], [{"t": "Str", "c": "local"}], ["/private/secret.png", ""]]},
                                         {"t": "RawInline", "c": ["latex", r"\input{/private/secret}"]},
                                         {"t": "Math", "c": [{"t": "InlineMath"}, r"\input{/private/secret}"]}]},
                {"t": "RawBlock", "c": ["latex", r"\input{/private/secret}"]},
                {"t": "Div", "c": [["", [], []], [{"t": "Para", "c": [{"t": "Image", "c": [["", [], []], [], ["https://invalid.test/secret.png", ""]]}]}]]},
            ]}


class ExportSecurityTests(unittest.TestCase):
    def test_ast_blocks_original_resources_raw_and_math_even_if_reader_returns_them(self):
        result = E._sanitize_pandoc_document(document())
        self.assertEqual(result["meta"], {})
        forbidden = {"Image", "RawBlock", "RawInline", "Math"}
        self.assertFalse(any(node["t"] in forbidden for node in nodes(result)))
        self.assertNotIn("https://invalid.test/secret.png", json.dumps(result))
        self.assertNotIn("/private/secret.png", json.dumps(result))
        self.assertTrue(any(node["t"] == "CodeBlock" for node in nodes(result)))

    def test_title_and_caption_escape_all_tex_special_characters_once(self):
        raw = "\\{}$&#%_^~\n\\input{private}"
        escaped = E._tex_text(raw)
        expected = (r"\textbackslash{}\{\}\$\&\#\%\_\textasciicircum{}\textasciitilde{}"
                    r" \textbackslash{}input\{private\}")
        self.assertEqual(escaped, expected)
        self.assertIn(expected, E.front_matter_md(raw, raw))
        self.assertIn(expected, E._chart_latex("chart_1.png", raw))
        with self.assertRaises(ValueError):
            E._chart_latex("../private.png", "caption")

    def test_pdf_writer_receives_only_sanitized_ast_and_trusted_generated_chart(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "report.md"
            source.write_text("# Original\n\\input{/private/secret}\n![x](https://invalid.test/x.png)\n", encoding="utf-8")
            chart = png(root / "_charts" / "generated.png")
            def run(command, **kwargs):
                calls.append((command, kwargs))
                if "--to=json" in command:
                    return types.SimpleNamespace(returncode=0, stdout=json.dumps(document()), stderr="")
                (Path(kwargs["cwd"]) / "report.pdf").write_bytes(b"%PDF-1.7\nfixture")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(E, "_which", return_value="pandoc"), patch.object(E.subprocess, "run", side_effect=run):
                E.to_pdf(source, root / "result.pdf", title=r"title\input{private}",
                         charts=[{"image": chart, "after": "## Generated", "caption": r"Cost $ \input{private}"}])
            self.assertEqual(len(calls), 2)
            parse, render = calls
            self.assertIn("--from=" + E.SAFE_MARKDOWN_READER, parse[0])
            self.assertIn("--from=json", render[0])
            self.assertIn("--pdf-engine-opt=-no-shell-escape", render[0])
            result = json.loads(render[1]["input"])
            raws = [node["c"][-1] for node in nodes(result) if node["t"] == "RawBlock"]
            self.assertEqual(len(raws), 1)
            self.assertIn(r"\includegraphics[width=0.94\linewidth,height=0.60\textheight,keepaspectratio]{chart_1.png}", raws[0])
            self.assertIn(r"\begin{minipage}{\linewidth}", raws[0])
            self.assertNotIn(r"\input{private}", raws[0])
            self.assertFalse(any(node["t"] in {"Image", "Math"} for node in nodes(result)))
            self.assertTrue(all(node["c"] == ["latex", r"\allowbreak{}"] for node in nodes(result) if node["t"] == "RawInline"))
            self.assertEqual(set(result["meta"]), {"title"})
            self.assertEqual(source.read_text(encoding="utf-8"), "# Original\n\\input{/private/secret}\n![x](https://invalid.test/x.png)\n")
            self.assertFalse(list(root.glob(".pdf-*")))

    def test_generated_chart_must_be_contained_and_anchor_must_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported, working = root / "exports", root / "exports" / "working"
            working.mkdir(parents=True)
            outside = png(root / "outside.png")
            with self.assertRaisesRegex(ValueError, "导出目录"):
                E._attach_pdf_charts(E._sanitize_pandoc_document(document()),
                                     [{"image": outside, "after": "## Generated"}], working, exported)
            inside = png(exported / "inside.png")
            with self.assertRaisesRegex(ValueError, "锚点"):
                E._attach_pdf_charts(E._sanitize_pandoc_document(document()),
                                     [{"image": inside, "after": "## Not present"}], working, exported)
            link = exported / "link.png"
            link.symlink_to(inside)
            with self.assertRaisesRegex(ValueError, "生成的 PNG"):
                E._attach_pdf_charts(E._sanitize_pandoc_document(document()),
                                     [{"image": link, "after": "## Generated"}], working, exported)

    @unittest.skipUnless(shutil.which("pandoc"), "Pandoc reader integration requires the declared system dependency")
    def test_real_reader_disables_raw_attribute_math_metadata_and_all_image_forms(self):
        payload = r'''---
header-includes: |
  \input{/private/secret}
---

# Original

\input{/private/secret}

```{=latex}
\input{/private/secret}
```

`\input{/private/secret}`{=latex}

$\input{/private/secret}$

\(\input{/private/secret}\)

![local](/private/secret.png)

![network](https://invalid.test/secret.png)

![reference][private]

[private]: file:///private/secret.png

<img src="/private/secret.png" />
'''
        parsed = E._pandoc_document(shutil.which("pandoc"), payload, E.SAFE_MARKDOWN_READER)
        self.assertFalse(any(node["t"] in {"RawBlock", "RawInline", "Math"} for node in nodes(parsed)))
        safe = E._sanitize_pandoc_document(parsed)
        self.assertFalse(any(node["t"] in {"RawBlock", "RawInline", "Math", "Image"} for node in nodes(safe)))
        self.assertEqual(safe["meta"], {})

    def test_generic_pdf_uses_same_safe_pipeline_and_keeps_separate_generated_charts(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = {"analysis_id": "a" * 32, "context": {"period": "2026-11"},
                        "report": {"markdown": "# Cost report\n\n## 三、成本对象与成本结构\n\n$\\input{secret}$\n"}}
            def chart_fixture(_snapshot, folder):
                image = png(folder / "chart.png")
                return [{"image": image, "path": image, "after": "## 三、成本对象与成本结构", "caption": "Generated"}]
            with patch.object(CO, "_charts", side_effect=chart_fixture), patch.object(CO.shutil, "which", return_value="available"), \
                    patch.object(E, "to_pdf", return_value=Path(directory) / "result.pdf") as render:
                CO.export_snapshot(snapshot, "pdf", directory)
            args, kwargs = render.call_args
            self.assertEqual(args[0].read_text(encoding="utf-8"), snapshot["report"]["markdown"])
            self.assertEqual(len(kwargs["charts"]), 1)
            self.assertIn("graphicx", kwargs["latex_header"])
            self.assertEqual(kwargs["shift_heading_level_by"], 0)


class FrozenThemeChartTests(unittest.TestCase):
    def frozen(self, theme, element="直接材料"):
        return {"product": "产品甲", "month": "2026-06", "period_kind": theme,
                "period_label": "2026-Q2" if theme == "quarterly" else "2026-06",
                "facts": {name: {"value": value} for name, value in [
                    ("本月单位成本", 10), ("本月材料成本", 6), ("本月人工成本", 1.5), ("本月制造成本", 2.5)]},
                "monthly": [{"月份": "2026-05", "单位成本": "9.5"}, {"月份": "2026-06", "单位成本": "10"}],
                "peer": [{"维度": "直接材料", "中药一厂": "6", "中药二厂": "6.5"}],
                "topic_element": element,
                "topic_rows": [{"月份": month, "产品名称": "产品甲", "直接材料(元/盒)": materials,
                                "直接人工(元/盒)": labor, "制造费用(元/盒)": overhead}
                               for month, materials, labor, overhead in [
                                   ("2026-05", "5.5", "1.0", "3.0"), ("2026-06", "6", "1.5", "2.5")]]}

    @unittest.skipUnless((ROOT / "references/templates/月度成本分析报告模板.md").is_file(),
                         "Legacy templates are not distributed in this snapshot")
    def test_all_three_report_themes_have_real_template_anchors_and_embedded_docx_charts(self):
        import chart_render as CR
        from docx import Document
        for theme, filename in [("monthly", "月度成本分析报告模板.md"),
                                ("quarterly", "季度成本分析报告模板.md"),
                                ("topic", "专题分析报告模板.md")]:
            with self.subTest(theme=theme), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                frozen = self.frozen(theme)
                context = {"theme": theme, "product": "产品甲", "as_of": "2026-06",
                           "period": frozen["period_label"], "export_facts": frozen}
                def generated(*args, **kwargs):
                    return png(next(arg for arg in args if isinstance(arg, Path)))
                with patch.object(CR, "render_structure", side_effect=generated) as structure, \
                        patch.object(CR, "render_trend", side_effect=generated), patch.object(CR, "render_peer", side_effect=generated):
                    source = root / "saved-report-with-opaque-id.md"
                    source.write_text((ROOT / "references/templates" / filename).read_text(encoding="utf-8"), encoding="utf-8")
                    outputs = E.export(source, "docx", root / "out", with_charts=True, context=context)
                doc = Document(outputs[0])
                self.assertEqual(len(doc.inline_shapes), 3)
                label = structure.call_args.kwargs["period_label"]
                self.assertIn("季度加权" if theme == "quarterly" else "截至月当月" if theme == "topic" else "当月", label)
                self.assertNotIn("本月", label)

    def test_topic_line_uses_each_selected_element_frozen_rows(self):
        import chart_render as CR
        for element in ("直接材料", "直接人工", "制造费用"):
            with self.subTest(element=element), tempfile.TemporaryDirectory() as directory:
                frozen = self.frozen("topic", element)
                with patch.object(CR, "render_structure", return_value=Path("structure.png")), \
                        patch.object(CR, "render_peer", return_value=Path("peer.png")), \
                        patch.object(CR, "render_trend", return_value=Path("trend.png")) as trend:
                    charts = E.build_report_charts("产品甲", "2026-06", Path(directory),
                        {"theme": "topic", "export_facts": frozen})
                self.assertEqual(trend.call_args.args[0], [
                    {"月份": row["月份"], "单位要素成本": row[f"{element}(元/盒)"]} for row in frozen["topic_rows"]])
                self.assertEqual(trend.call_args.kwargs["series_keys"], [("单位要素成本", element)])
                self.assertEqual(charts[1]["after"], "### 2.2 截至期分月明细")
                self.assertEqual(charts[2]["after"], "## 四、同口径对标")
                self.assertIn("当月", charts[0]["caption"])
                self.assertIn("当月", charts[2]["caption"])

    def test_no_frozen_data_never_falls_back_to_current_csv(self):
        with self.assertRaisesRegex(ValueError, "冻结"):
            E.build_report_charts("产品甲", "2026-06", context={"theme": "topic"})

    def test_topic_rejects_future_or_wrong_product_frozen_rows(self):
        import chart_render as CR
        for field, value in (("月份", "2026-07"), ("产品名称", "别的产品")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                frozen = self.frozen("topic")
                frozen["topic_rows"][1][field] = value
                with patch.object(CR, "render_structure", return_value=Path("structure.png")):
                    with self.assertRaises(ValueError):
                        E.build_report_charts("产品甲", "2026-06", Path(directory), {"theme": "topic", "export_facts": frozen})


if __name__ == "__main__":
    unittest.main()
