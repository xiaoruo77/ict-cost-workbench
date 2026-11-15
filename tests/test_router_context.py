"""Verify that one answer cannot mix independently updated state versions."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router
import state


class RouterContextTests(unittest.TestCase):
    def workspace(self, base, name, amount):
        root = Path(base) / name
        (root / "wiki").mkdir(parents=True)
        (root / "wiki/product.md").write_text(
            "# Product\n| 产品名称 | 月份 | 成本(元) |\n|---|---|---|\n"
            f"| Widget | 2026-05 | {amount} |\n", encoding="utf-8")
        return root, state.build(False, workspace_root=root)

    def test_answer_keeps_the_verified_state_and_resets_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            first, doc = self.workspace(folder, "first", 11)
            second, _ = self.workspace(folder, "second", 22)
            poisoned = copy.deepcopy(doc)
            poisoned["metrics"][0]["value"] = 999
            with patch.object(state, "load", side_effect=[doc, poisoned]) as read:
                reply = router.answer("Widget 2026-05 成本多少", workspace_root=first)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(reply["rows"][0]["value"], 11)
            self.assertNotIn("999", reply["answer"])
            self.assertIsNone(router._VERIFIED_DOC.get())
            reply = router.answer("Widget 2026-05 成本多少", workspace_root=second)
            self.assertEqual(reply["rows"][0]["value"], 22)

    def test_question_with_unavailable_period_cannot_return_other_month(self):
        with tempfile.TemporaryDirectory() as folder:
            root, _ = self.workspace(folder, "first", 11)
            reply = router.answer("Widget 2026-04 成本多少", workspace_root=root)
            self.assertEqual(reply["level"], "unavailable")
            self.assertNotIn("=11", reply["answer"])


if __name__ == "__main__":
    unittest.main()
