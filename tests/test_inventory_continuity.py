from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from costing_inventory_continuity import (
    native_warehouse_close, preview_rollforward, WarehouseBalance, CarriedReceipt, TransitPosition,
    SnapshotPin, InventoryScope, ContinuityBinding, ContinuityUnavailable, ContinuityMismatch,
)
from costing_outputs import create_snapshot, load_snapshot
from cost_core.costing import CostingEngine
from test_inventory_transfer import case, evaluate
from test_composite_adapter import text
from test_composite_meter_v3 import two_accounts


def sha(s):
    return sha256(s.encode()).hexdigest()


class LocalInjectedAuthority:
    """Independent fixture, not a live registry or a source of permissions."""
    def __init__(self, binding):
        self.binding=binding
        self.calls=[]

    def read_continuity(self, **kw):
        self.calls.append(kw)
        return self.binding


def snapshot_and_bind(root):
    schema, rows = case()
    _, r = evaluate(schema,rows)
    ds=root/"costing_datasets"/schema["dataset_id"]
    ds.mkdir(parents=True)
    (ds/"data.csv").write_text(text(rows),encoding="utf-8")
    (ds/"manifest.json").write_text(json.dumps({"schema":schema}),encoding="utf-8")
    ss=create_snapshot(r,root,root,dict(dataset_id=schema["dataset_id"],dataset_label="Standalone native close",
                                        period="2026-11",scenario="actual",owner_external_id="synthetic-owner"))
    snap=load_snapshot(ss["analysis_id"],root)
    pin=SnapshotPin(snap["analysis_id"],snap["revision"])
    scope=InventoryScope("synthetic-owner","warehouse-ledger-uuid","Cold_store","cold-chain-inventory")
    w,pin_sha=native_warehouse_close(snapshot=snap,prior_pin=pin,scope=scope,current_period="2026-12")
    t=(TransitPosition("PENDING-7","WH_A","WH_B","CRATE","EA","4","40.00","2026-11-29T08:00:00Z",
                       "EXTERNAL-DISPATCH-77",sha("independent dispatch 77")),
       TransitPosition("PENDING-8","WH_B","WH_C","CRATE","EA","1.25","17.23","2026-10-27T08:00:00Z",
                       "EXTERNAL-DISPATCH-78",sha("independent dispatch 78")))
    r=(CarriedReceipt("PENDING-7","WH_B","CRATE","EA","4","2026-12-02T08:00:00Z",1,
                     "EXTERNAL-RECEIPT-51","EXTERNAL-DISPATCH-77",sha("independent receipt 51")),)
    b=ContinuityBinding(scope,pin,"2026-12","independent registry version 47",sha("policy config"),pin_sha,
                        sha("independent boundary population verified"),sha("independent receipt population verified"),t,r)
    return dict(snapshot=snap,prior_pin=pin,scope=scope,current_period="2026-12",declared_warehouse=w,
                declared_transit=t,reported_receipts=r,authority=LocalInjectedAuthority(b))


class InventoryContinuity(unittest.TestCase):
    def test_real_native_snapshot_projection_read_only_same_receipts(self):
        with TemporaryDirectory() as t:
            root=Path(t)
            args=snapshot_and_bind(root)
            received=args["declared_warehouse"]
            self.assertEqual([(w.warehouse,w.quantity,w.carrying_value) for w in received],
                             [("WH_A","6","60.00"),("WH_B","11","188.57"),("WH_C","1","17.14")])
            file_state={p:p.read_bytes() for p in root.rglob("*") if p.is_file()}
            first=preview_rollforward(**args)
            second=preview_rollforward(**{**args,"declared_warehouse":tuple(reversed(received)),
                                          "declared_transit":tuple(reversed(args["declared_transit"]))})
            self.assertEqual(first,second)
            self.assertEqual(args["authority"].calls[0],dict(scope=args["scope"],prior_pin=args["prior_pin"],current_period="2026-12"))
            self.assertEqual(first["status"],"matched_read_only_preview")
            self.assertEqual(first["arrivals_to_stage"][0]["asset_reclassification_value"],"40.00")
            self.assertEqual(first["arrivals_to_stage"][0]["reported_receipt"]["quantity"],"4")
            self.assertEqual(first["prior_transit_remaining_after_preview"][0]["carrying_value"],"17.23")
            self.assertEqual(first["opening_transit"][1]["dispatch_at"],"2026-10-27T08:00:00Z")
            self.assertEqual(first["expense_created_by_preview"],"0.00")
            self.assertFalse(first["can_claim_complete"] or first["posts_entries"] or first["publishes_close"])
            self.assertEqual(file_state,{p:p.read_bytes() for p in root.rglob("*") if p.is_file()})
            serialized=json.dumps(first)
            self.assertNotIn("registry version",serialized)
            self.assertNotIn("warehouse-ledger-uuid",serialized)
            self.assertNotIn("synthetic-owner",serialized)

    def test_cas_fingerprints_bind_prior_authority_policy_and_exact_receipt_plan(self):
        with TemporaryDirectory() as t:
            a=snapshot_and_bind(Path(t))
            init=preview_rollforward(**a)
            b=a["authority"].binding
            refreshed=preview_rollforward(**{**a,"authority":LocalInjectedAuthority(
                replace(b,accepted_close_token="registry new close token"))})
            self.assertNotEqual(init["precondition_sha256"],refreshed["precondition_sha256"])
            self.assertNotEqual(init["proposal_sha256"],refreshed["proposal_sha256"])
            moved=replace(a["reported_receipts"][0],receipt_evidence_sha256=sha("new independent receipt reference"))
            r=(moved,)
            comparison=preview_rollforward(**{**a,"reported_receipts":r,"authority":LocalInjectedAuthority(
                replace(b,receipts=r) )})
            self.assertNotEqual(init["precondition_sha256"],comparison["precondition_sha256"])
            self.assertNotEqual(init["proposal_sha256"],comparison["proposal_sha256"])
            self.assertFalse(comparison["publishes_close"])

    def test_real_composite_requires_explicit_scoped_inventory_branch(self):
        with TemporaryDirectory() as t:
            path=Path(t); schema, rows=two_accounts()
            stand, movements=case(); inv=stand["raw_inventory"]
            book=schema["raw_composite"]["scope"]["book"]
            inv["context"]["book"]=book
            for stream in inv["streams"]: stream["opening"]["book"]=book
            for mv in movements: mv.update(book=book,input_name="stock",posting_identity=mv["id"])
            schema["raw_composite"]["inputs"]["stock"]={"raw_inventory":inv}
            rows=[row for row in rows if row["input_name"]!="stock"]+movements
            _,res=evaluate(schema,rows)
            ds=path/"costing_datasets"/schema["dataset_id"]; ds.mkdir(parents=True)
            (ds/"data.csv").write_text(text(rows),encoding="utf-8")
            (ds/"manifest.json").write_text(json.dumps({"schema":schema}),encoding="utf-8")
            ss=create_snapshot(res,path,path,dict(dataset_id=schema["dataset_id"],dataset_label="Synthetic composite",
                                                 period="2026-11",scenario="actual",owner_external_id="synthetic-owner"))
            snap=load_snapshot(ss["analysis_id"],path)
            scope=InventoryScope("synthetic-owner","composite-book",book,"inventory-1",branch="stock")
            pin=SnapshotPin(snap["analysis_id"],snap["revision"])
            balances,digest=native_warehouse_close(snapshot=snap,prior_pin=pin,scope=scope,current_period="2026-12")
            self.assertEqual([b.carrying_value for b in balances],["60.00","188.57","17.14"])
            for wrong in (None,"power","STOCK"):
                with self.subTest(wrong=wrong),self.assertRaises(ContinuityUnavailable):
                    native_warehouse_close(snapshot=snap,prior_pin=pin,scope=replace(scope,branch=wrong),current_period="2026-12")
            a=LocalInjectedAuthority(ContinuityBinding(scope,pin,"2026-12","synthetic view",sha("policy"),digest,
                                                      sha("boundary"),sha("arrival"),(),()))
            p=preview_rollforward(snapshot=snap,prior_pin=pin,scope=scope,current_period="2026-12",
                                  declared_warehouse=balances,declared_transit=(),reported_receipts=(),authority=a)
            self.assertFalse(p["publishes_close"] or p["arrivals_to_stage"])

    def test_old_versions_scope_tamper_and_provider_fail_closed(self):
        with TemporaryDirectory() as t:
            a=snapshot_and_bind(Path(t))
            b=a["authority"].binding
            corrupt=deepcopy(a["snapshot"])
            corrupt["result"]["recognition"]["inventory_valuation"]["streams"][0]["next_state"]["value"]="60.01"
            negatives=[
                {"authority":None},{"authority":{**{f:getattr(b,f) for f in b.__slots__}}},
                {"snapshot":corrupt},{"current_period":"2027-01"},{"scope":replace(a["scope"],owner="other-tenant")},
                {"scope":replace(a["scope"],branch="invented")},
                {"prior_pin":replace(a["prior_pin"],revision=sha("unrelated"))},
                {"authority":LocalInjectedAuthority(replace(b,warehouse_close_sha256=sha("fake warehouse")))},
                {"authority":LocalInjectedAuthority(replace(b,scope=replace(a["scope"],inventory_uid="wrong")))}
            ]
            for k,delta in enumerate(negatives):
                with self.subTest(k=k),self.assertRaises(ContinuityUnavailable):
                    preview_rollforward(**{**a,**delta})
            class Broken:
                def read_continuity(self, **kwargs):
                    raise RuntimeError("private upstream diag")
            with self.assertRaises(ContinuityUnavailable) as exc:
                preview_rollforward(**{**a,"authority":Broken()})
            self.assertNotIn("private upstream",str(exc.exception))

    def test_user_cannot_forge_arrivals_or_change_stock_or_carry_amount(self):
        with TemporaryDirectory() as t:
            a=snapshot_and_bind(Path(t))
            w,tpos,r=(a[k] for k in ("declared_warehouse","declared_transit","reported_receipts"))
            for k,delta in enumerate((
                {"declared_warehouse":(replace(w[0],carrying_value="40"),)+w[1:]},
                {"declared_warehouse":(WarehouseBalance("WH_A","CRATE","EA","6","60",sha("wrong state")),)+w[1:]},
                {"declared_transit":(replace(tpos[0],carrying_value="40.01"),)+tpos[1:]},
                {"declared_transit":tpos[:-1]},
                {"declared_transit":(replace(tpos[0],dispatch_evidence_sha256=sha("altered")),)+tpos[1:]},
                {"declared_transit":(replace(tpos[0],transfer_id="AtoB"),)+tpos[1:]},
                {"declared_transit":(replace(tpos[0],dispatch_source_id="DOC1"),)+tpos[1:]},
                {"reported_receipts":(replace(r[0],warehouse="WH_C"),)},
                {"reported_receipts":(replace(r[0],received_at="2026-11-30T23:59:59Z"),)},
                {"reported_receipts":(replace(r[0],quantity="3.99"),)},
                {"reported_receipts":(replace(r[0],receipt_source_id="DOC3"),)},
                {"reported_receipts":(replace(r[0],receipt_evidence_sha256=sha("fabricated")),)},
                {"reported_receipts":(r[0],replace(r[0],sequence=2,receipt_source_id="REPLAY"))},
                {"reported_receipts":()},
            )):
                with self.subTest(k=k),self.assertRaises(ContinuityMismatch):
                    preview_rollforward(**{**a,**delta})

    def test_aliases_rejected_even_when_mistakenly_included_in_typed_binding(self):
        with TemporaryDirectory() as t:
            a=snapshot_and_bind(Path(t))
            b=a["authority"].binding
            r=a["reported_receipts"][0]
            p=a["declared_transit"]
            for alias in ("external-dispatch-77","ＥＸＴＥＲＮＡＬ-ＤＩＳＰＡＴＣＨ-７７","doc1","ＤＯＣ３"):
                bad=(replace(r,receipt_source_id=alias),)
                with self.subTest(alias=alias),self.assertRaises(ContinuityMismatch) as exc:
                    preview_rollforward(**{**a,"reported_receipts":bad})
                self.assertNotIn(alias,str(exc.exception))
                with self.assertRaises(ContinuityUnavailable):
                    preview_rollforward(**{**a,"reported_receipts":bad,"authority":LocalInjectedAuthority(replace(b,receipts=bad))})
            for alias in ("external-dispatch-77","ＥＸＴＥＲＮＡＬ-ＤＩＳＰＡＴＣＨ-７７","doc1"):
                transit=p[:1]+(replace(p[1],dispatch_source_id=alias),)
                with self.subTest(alias=alias),self.assertRaises(ContinuityMismatch):
                    preview_rollforward(**{**a,"declared_transit":transit})
                with self.assertRaises(ContinuityUnavailable):
                    preview_rollforward(**{**a,"declared_transit":transit,"authority":LocalInjectedAuthority(replace(b,transit=transit))})

    def test_aliases_in_native_source_history_are_continuity_only_failure(self):
        with TemporaryDirectory() as t:
            root=Path(t); s,rows=case()
            s["raw_inventory"]["streams"][0]["opening"]["seen_source_ids"]=["doc1"]
            _,result=evaluate(s,rows)  # native semantics remain exact-case
            ds=root/"costing_datasets"/s["dataset_id"];ds.mkdir(parents=True)
            (ds/"data.csv").write_text(text(rows),encoding="utf-8")
            (ds/"manifest.json").write_text(json.dumps({"schema":s}),encoding="utf-8")
            ss=create_snapshot(result,root,root,dict(dataset_id=s["dataset_id"],dataset_label="Alias fixture",
                               period="2026-11",scenario="actual",owner_external_id="synthetic-owner"))
            self.assertEqual(load_snapshot(ss["analysis_id"],root)["revision"],ss["revision"])
            scope=InventoryScope("synthetic-owner","warehouse-ledger-uuid","Cold_store","cold-chain-inventory")
            with self.assertRaises(ContinuityUnavailable):
                native_warehouse_close(snapshot=ss,prior_pin=SnapshotPin(ss["analysis_id"],ss["revision"]),
                                       scope=scope,current_period="2026-12")

    def test_engine_object_scope_cannot_hide_behind_blank_snapshot_wrapper(self):
        with TemporaryDirectory() as t:
            root=Path(t); a=snapshot_and_bind(root)
            s,r=case()
            req,_=evaluate(s,r)
            for mode in ("real-filter","disagreeing-dataset-ids"):
                result=CostingEngine().analyze(req,cost_object="WH_C") if mode=="real-filter" else CostingEngine().analyze(req)
                if mode=="disagreeing-dataset-ids": result["scope"]["object_ids"]=["WH_C"]
                ss=create_snapshot(result,root,root,dict(dataset_id=s["dataset_id"],dataset_label="Scope fixture",
                                   period="2026-11",scenario="actual",owner_external_id="synthetic-owner"))
                self.assertEqual(load_snapshot(ss["analysis_id"],root)["revision"],ss["revision"])
                pin=SnapshotPin(ss["analysis_id"],ss["revision"])
                authority=LocalInjectedAuthority(replace(a["authority"].binding,prior_pin=pin))
                with self.subTest(mode=mode),self.assertRaises(ContinuityUnavailable):
                    preview_rollforward(**{**a,"snapshot":ss,"prior_pin":pin,"authority":authority})
                self.assertFalse(authority.calls)
