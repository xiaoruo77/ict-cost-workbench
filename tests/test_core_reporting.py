"""Offline report contracts and the legacy pipeline's real core integration."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import socket
import sys
import tempfile
import os
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cost_core import reporting as CR


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.offline = ExitStack()
        self.addCleanup(self.offline.close)
        self.calls = []

        def deny(*args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("No network is allowed in report contract tests")

        for endpoint in ("socket.create_connection", "socket.socket.connect",
                         "socket.getaddrinfo", "urllib.request.urlopen"):
            self.offline.enter_context(patch(endpoint, side_effect=deny))

    def tearDown(self):
        self.assertEqual(self.calls, [], "An offline test attempted a network request")

    def test_arbitrary_two_five_categories_units_and_unicode(self):
        for count, unit in ((2, "CNY/份"), (5, "EUR/kg")):
            with self.subTest(count=count, unit=unit):
                names = tuple(f"category-{number}" for number in range(count))
                profile = CR.ReportProfile(
                    profile_id="custom", components=names, units={"cost": unit},
                    templates={"monthly": "# {{报告名称}}\n\n{{类别表}}\n\n单位：{{单位}}"})
                request = CR.ReportRequest("object-a", "2026-05", dataset_id="custom-a")
                output = CR.ReportWorkflow().run(
                    request, profile,
                    prepare_facts=lambda req: {"missing": [], "amounts": dict.fromkeys(names, 2)},
                    prepare_values=lambda facts, req: {
                        "报告名称": "Generic cost summary", "单位": profile.units["cost"],
                        "类别表": "\n".join(f"{name}: {amount}" for name, amount in facts["amounts"].items()),
                    },
                    narrative=Mock(side_effect=AssertionError("Dry run must not invoke a model")))
                self.assertEqual(len(output["facts"]["amounts"]), count)
                self.assertIn(unit, output["report"])
                self.assertEqual(output["stats"]["llm_calls"], 0)
                for domain_word in ("药企", "中药", "GMP", "元/盒", "直接材料"):
                    self.assertNotIn(domain_word, output["report"])

    def test_rendering_distinguishes_zero_none_missing_and_percent(self):
        text, missing = CR.render_template(
            "{{零}}|{{未知}}|{{未提供}}|{{比例}}%|{{说明}}\n\n\n尾部",
            {"零": 0, "未知": None, "比例": "+2.75%", "说明": ""})
        self.assertEqual(text, "0|—|—|+2.75%|\n\n尾部")
        self.assertEqual(missing, ["未知", "未提供"])
        CR.require_complete_facts({"facts": {"zero": 0}}, ["zero"])
        with self.assertRaises(ValueError):
            CR.require_complete_facts({"facts": {"zero": 0, "unknown": None}}, ["unknown"])
        with self.assertRaises(ValueError):
            CR.ensure_unique_bindings([("same", "a"), ("same", "b")])

    def test_threshold_coverage_never_interprets_unknown_as_zero(self):
        result = CR.evaluate_threshold({"zero": 0, "missing": None}, 10)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["unknown"], ["missing"])
        self.assertEqual(result["coverage"], {"expected": 2, "observed": 1, "fraction": 0.5})
        self.assertEqual(result["within"], [("zero", "0")])
        self.assertEqual(CR.evaluate_threshold({}, 10)["status"], "unknown")
        self.assertEqual(CR.evaluate_threshold({"bad": float("nan")}, 10)["status"], "unknown")
        self.assertEqual(CR.evaluate_threshold({"boundary": 10}, 10, comparator="gt")["status"], "within")
        self.assertEqual(CR.evaluate_threshold({"boundary": 10}, 10)["status"], "breached")

    def test_missing_template_precedes_fact_and_model_work(self):
        facts = Mock(side_effect=AssertionError("Facts callback must not execute"))
        narrative = Mock(side_effect=AssertionError("Model callback must not execute"))
        profile = CR.ReportProfile(
            "missing", templates={"monthly": CR.ReportTemplate(path=Path(__file__).parent / "absent-template.md")},
            model_budget={"max_calls": 1})
        with self.assertRaises(FileNotFoundError):
            CR.ReportWorkflow().run(
                CR.ReportRequest("x", "2026-05", use_llm=True), profile,
                prepare_facts=facts, prepare_values=Mock(), narrative=narrative)
        facts.assert_not_called()
        narrative.assert_not_called()

    def test_declared_missing_facts_precede_model_and_render(self):
        narrative, renderer = Mock(), Mock()
        profile = CR.ReportProfile("missing-data", templates={"monthly": "{{value}}"},
                                   model_budget={"max_calls": 1})
        with self.assertRaises(ValueError):
            CR.ReportWorkflow().run(
                CR.ReportRequest("x", "2026-05", use_llm=True), profile,
                prepare_facts=lambda req: {"missing": ["needed-record"]},
                prepare_values=Mock(), narrative=narrative, renderer=renderer)
        narrative.assert_not_called()
        renderer.assert_not_called()

    def test_budget_and_required_narrative_fields_are_enforced_without_models(self):
        request = CR.ReportRequest("x", "2026-05", use_llm=True)
        profile = CR.ReportProfile("budget", fields={"summary": "plain text"},
                                   templates={"monthly": "{{summary}}"})
        callback = Mock()
        with self.assertRaises(ValueError):
            CR.ReportWorkflow().run(request, profile, prepare_facts=Mock(),
                                    prepare_values=Mock(), narrative=callback)
        callback.assert_not_called()
        profile = replace(profile, model_budget={"max_calls": 1})
        with self.assertRaises(ValueError):
            CR.ReportWorkflow().run(request, profile, prepare_facts=lambda req: {},
                                    prepare_values=lambda facts, req: {}, narrative=lambda facts, req: {})
        with self.assertRaises(ValueError):
            CR.ReportWorkflow().run(request, profile, prepare_facts=lambda req: {},
                                    prepare_values=lambda facts, req: {},
                                    narrative=lambda facts, req: {"summary": "stub", "_calls": 2})
        output = CR.ReportWorkflow().run(
            request, profile, prepare_facts=lambda req: {}, prepare_values=lambda facts, req: {},
            narrative=lambda facts, req: {"summary": "stub", "_calls": 1})
        self.assertEqual(output["report"], "stub")

    def test_semantic_profile_guard_rejects_wrong_dataset_and_future_records(self):
        def scoped(context):
            if context.facts["dataset"] != context.request.dataset_id:
                raise ValueError("Wrong dataset")
            if any(period > context.request.as_of for period in context.facts["periods"]):
                raise ValueError("Future record")

        profile = CR.ReportProfile("scoped", templates={"monthly": "{{value}}"},
                                   semantic_guards=(scoped,), model_budget={"max_calls": 1})
        request = CR.ReportRequest("x", "2026-05", use_llm=True, as_of="2026-05", dataset_id="one")
        for data in ({"dataset": "two", "periods": ["2026-05"]},
                     {"dataset": "one", "periods": ["2026-06"]}):
            callback = Mock(side_effect=AssertionError("Guard must precede narrative work"))
            with self.assertRaises(ValueError):
                CR.ReportWorkflow().run(request, profile, prepare_facts=lambda req: data,
                                        prepare_values=lambda facts, req: {"value": 0}, narrative=callback)
            callback.assert_not_called()

    def test_legacy_alias_and_build_really_use_shared_workflow_and_renderer(self):
        import report_build as legacy
        import grounding as old_grounding
        from industry_plugins.pharma import reporting as pharma
        from industry_plugins.pharma import grounding as pharma_grounding

        self.assertIs(legacy, pharma)
        self.assertIs(old_grounding, pharma_grounding)
        facts = SimpleNamespace(
            product="fixture-object", month="2026-05", monthly=[{"月份": "2026-05"}],
            facts={key: SimpleNamespace(value=0, display="0.00%") for _, key in legacy.ELEMENT_FACTS},
            missing=[], conflicts=[])
        # Keep fixtures in an explicitly writable scratch root, not read-only source.
        temp_parent = Path(os.environ.get("COST_TEST_TMP", tempfile.gettempdir()))
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as temp:
            self.assertTrue(Path(temp).resolve().is_relative_to(temp_parent.resolve()))
            template = Path(temp) / "template.md"
            template.write_text("# {{title}}\n{{cost}}\n{{重点分析段落}}", encoding="utf-8")
            original = CR.ReportWorkflow.run
            with patch.object(legacy, "TEMPLATES", {"monthly": template}), \
                 patch.object(legacy.RF, "build", return_value=facts), \
                 patch.object(legacy, "deterministic_map", return_value={"title": "fixture", "cost": 0}), \
                 patch.object(legacy, "_llm_client", side_effect=AssertionError("No LLM client construction")), \
                 patch.object(legacy, "generate_analysis", side_effect=AssertionError("No model invocation")), \
                 patch.object(CR.ReportWorkflow, "run", autospec=True, side_effect=original) as workflow, \
                 patch.object(CR, "render_template", wraps=CR.render_template) as renderer:
                output = legacy.build("fixture-object", "2026-05", use_llm=False, verbose=False)
            self.assertEqual(workflow.call_count, 1)
            self.assertEqual(renderer.call_count, 1)
            self.assertEqual(set(output), {"report", "facts", "stats", "analysis", "docx"})
            self.assertEqual(output["report"], "# fixture\n0\n")
            self.assertEqual(output["stats"]["llm_calls"], 0)
            self.assertIs(output["facts"], facts)
            self.assertEqual(legacy.evaluate_threshold(SimpleNamespace(facts={}))['status'], "unknown")
            self.assertNotIn("无成本要素触发告警", legacy.threshold_verdict(
                legacy.evaluate_threshold(SimpleNamespace(facts={}))))


if __name__ == "__main__":
    unittest.main()
