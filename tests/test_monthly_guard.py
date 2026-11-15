import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from costing_monthly_guard import (
    MonthlyBindingClaim, MonthlyBindingPair, MonthlyPreviewIncomparable,
    MonthlyPreviewUnavailable, MonthlyRevisionPin, guarded_monthly_preview,
)
from test_month_comparison import _BOOK, _OWNER, _POLICY, snapshot


class Authority:
    def __init__(self, batch):
        self.batch = batch
        self.calls = []

    def load_pair(self, **args):
        self.calls.append(args)
        if isinstance(self.batch, Exception):
            raise self.batch
        return self.batch


class GuardContract(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.previous = snapshot(self.root, "2026-12", "a")
        self.current = snapshot(self.root, "2027-01", "b", True)
        self.previous_pin = MonthlyRevisionPin(self.previous["analysis_id"], self.previous["revision"])
        self.current_pin = MonthlyRevisionPin(self.current["analysis_id"], self.current["revision"])
        self.batch = MonthlyBindingPair(
            MonthlyBindingClaim(_OWNER, self.current_pin, "book-registry-uuid-A", _BOOK, _POLICY),
            MonthlyBindingClaim(_OWNER, self.previous_pin, "book-registry-uuid-A", _BOOK, _POLICY),
        )
        self.authority = Authority(self.batch)

    def call(self, **overrides):
        args = dict(current_snapshot=self.current, previous_snapshot=self.previous,
                    current_pin=self.current_pin, previous_pin=self.previous_pin,
                    expected_owner=_OWNER, authority=self.authority)
        args.update(overrides)
        return guarded_monthly_preview(**args)

    def test_narrow_success_read_only_one_atomic_authority_view(self):
        old = copy.deepcopy((self.previous, self.current))
        files = {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.call()
        self.assertEqual("30.00", result["comparable_variance"])
        self.assertFalse(result["can_claim_complete"])
        self.assertEqual(1, len(self.authority.calls))
        self.assertEqual(self.authority.calls[0], dict(
            expected_owner=_OWNER, current=self.current_pin, previous=self.previous_pin))
        response = json.dumps(result)
        for private_item in (_OWNER, _POLICY, _BOOK, self.batch.current.book_uid, "data.csv", "raw_rows"):
            self.assertNotIn(private_item, response)
        self.assertEqual(old, (self.previous, self.current))
        self.assertEqual(files, {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_none_foreign_stale_invalid_corrupt_deny_before_provider_call(self):
        corrupt = copy.deepcopy(self.current)
        corrupt["result"]["totals"]["total_cost"] = "10000.00"
        for kwargs in (
            dict(authority=None), dict(expected_owner="someone-else"),
            dict(current_snapshot=corrupt), dict(current_snapshot={}),
            dict(current_pin=replace(self.current_pin, revision="a"*64)),
            dict(current_pin=replace(self.current_pin, analysis_id="A"*32)),
            dict(current_pin={}), dict(current_snapshot=self.previous),
        ):
            with self.subTest(kwargs=tuple(kwargs)), self.assertRaises(MonthlyPreviewUnavailable):
                self.call(**kwargs)
        self.assertEqual([], self.authority.calls)

    def test_malformed_missing_foreign_wrong_scope_provider_are_uniform(self):
        invalids = [
            None, {},
            TimeoutError("private trusted registry address"),
            replace(self.batch, current=None),
            replace(self.batch, current=replace(self.batch.current, owner="other-owner")),
            replace(self.batch, current=replace(self.batch.current, pin=self.previous_pin)),
            replace(self.batch, current=replace(self.batch.current, book_uid="\u0000")),
            replace(self.batch, current=replace(self.batch.current, declared_book="same enterprise other book")),
            replace(self.batch, current=replace(self.batch.current, policy_sha256="A"*64)),
        ]
        for invalid in invalids:
            with self.subTest(value=type(invalid)), self.assertRaises(MonthlyPreviewUnavailable) as caught:
                self.call(authority=Authority(invalid))
            self.assertEqual("", str(caught.exception))

    def test_no_merge_even_when_display_book_is_same_or_only_policy_differs(self):
        for change in (dict(book_uid="separate-registered-ledger"), dict(policy_sha256="f"*64)):
            provider = Authority(replace(self.batch, current=replace(self.batch.current, **change)))
            with self.assertRaises(MonthlyPreviewIncomparable):
                self.call(authority=provider)

    def test_same_revision_cannot_be_adjacent(self):
        provider = Authority(replace(self.batch, current=self.batch.previous))
        with self.assertRaises(MonthlyPreviewIncomparable):
            self.call(authority=provider, current_snapshot=self.previous, current_pin=self.previous_pin)


if __name__ == "__main__":
    unittest.main()
