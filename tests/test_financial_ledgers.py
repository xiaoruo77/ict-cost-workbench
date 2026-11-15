"""Independent two-period financial contracts and failure regressions."""
import copy
import csv
import hashlib
import io
import json
import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

from cost_core.business_views import compile_business_views, evaluate_business_views

BOOK = "SYNTH-FINANCIAL"
START = "2026-10"
POLICY = "synthetic approved policy FIN-V1; not a real company approval"


def csv_text(fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def table(fields, tuples, period):
    result = []
    for values in tuples:
        row = dict(zip(fields, values))
        if row["period"] <= period:
            result.append({"book": BOOK, "currency": "CNY", **row})
    return csv_text(["book", "currency"] + fields, result)


def costs(period):
    return ({"fuel": "200.00", "depreciation": "200.00", "amortization": "100.00",
             "labor": "400.00", "carrier_subcontract": "700.00"} if period == START else
            {"fuel": "312.00", "depreciation": "200.00", "amortization": "100.00",
             "service_release": "300.00", "labor": "500.00", "carrier_subcontract": "350.00"})


def fixture(period="2026-11"):
    sf = ["id", "account_id", "kind", "op", "amount", "ref", "status", "evidence", "period"]
    sr = [
        ("AR-OPEN", "AR", "receivable", "opening", "0.00", "APP-AR", "explicit_zero", POLICY, START),
        ("AR-OCT-INVOICE", "AR", "receivable", "invoice", "1000.00", "", "data", POLICY, START),
        ("AR-OCT-CASH", "AR", "receivable", "receipt", "200.00", "AR-OCT-INVOICE", "data", POLICY, START),
        ("AP-OPEN", "AP", "payable", "opening", "0.00", "APP-AP", "explicit_zero", POLICY, START),
        ("AP-OCT-INVOICE", "AP", "payable", "invoice", "700.00", "", "data", POLICY, START),
        ("AP-OCT-CASH", "AP", "payable", "payment", "200.00", "AP-OCT-INVOICE", "data", POLICY, START),
        ("COD-OPEN", "COD", "agency", "opening", "0.00", "APP-COD", "explicit_zero", POLICY, START),
        ("COD-OCT-COLLECT", "COD", "agency", "collect", "1000.00", "", "data", POLICY, START),
        ("COD-OCT-REMIT", "COD", "agency", "remit", "600.00", "COD-OCT-COLLECT", "data", POLICY, START),
        ("AR-NOV-INVOICE", "AR", "receivable", "invoice", "600.00", "", "data", POLICY, "2026-11"),
        ("AR-NOV-CREDIT", "AR", "receivable", "credit", "100.00", "AR-OCT-INVOICE", "data", POLICY, "2026-11"),
        ("AR-NOV-CASH1", "AR", "receivable", "receipt", "500.00", "AR-OCT-INVOICE", "data", POLICY, "2026-11"),
        ("AR-NOV-CASH2", "AR", "receivable", "receipt", "300.00", "AR-NOV-INVOICE", "data", POLICY, "2026-11"),
        ("AP-NOV-INVOICE", "AP", "payable", "invoice", "400.00", "", "data", POLICY, "2026-11"),
        ("AP-NOV-CREDIT", "AP", "payable", "credit", "50.00", "AP-OCT-INVOICE", "data", POLICY, "2026-11"),
        ("AP-NOV-CASH1", "AP", "payable", "payment", "300.00", "AP-OCT-INVOICE", "data", POLICY, "2026-11"),
        ("AP-NOV-CASH2", "AP", "payable", "payment", "100.00", "AP-NOV-INVOICE", "data", POLICY, "2026-11"),
        ("COD-NOV-COLLECT", "COD", "agency", "collect", "500.00", "", "data", POLICY, "2026-11"),
        ("COD-NOV-REMIT1", "COD", "agency", "remit", "300.00", "COD-OCT-COLLECT", "data", POLICY, "2026-11"),
        ("COD-NOV-REMIT2", "COD", "agency", "remit", "200.00", "COD-NOV-COLLECT", "data", POLICY, "2026-11"),
        ("COD-NOV-REFUND", "COD", "agency", "refund", "100.00", "COD-NOV-COLLECT", "data", POLICY, "2026-11"),
    ]
    af = ["id", "asset_id", "op", "quantity", "unit", "amount", "ref", "status", "evidence", "period"]
    ar = [
        ("FUEL-OCT-USE", "FUEL", "consume", "20", "L", "", "OPEN-FUEL", "computed", POLICY, START),
        ("FLEET-OCT-DEP", "FLEET", "depreciate", "0", "asset", "200.00", "OPEN-FLEET", "data", POLICY, START),
        ("PRE-OCT-AMORT", "PRE", "amortize", "1", "month", "100.00", "OPEN-PRE", "data", POLICY, START),
        ("SERVICE-OCT-CAP", "SERVICE", "capitalize", "1", "job", "300.00", "OPEN-SERVICE", "data", POLICY, START),
        ("FUEL-NOV-BUY", "FUEL", "acquire", "20", "L", "240.00", "", "data", POLICY, "2026-11"),
        ("FUEL-NOV-USE", "FUEL", "consume", "30", "L", "", "FUEL-NOV-BUY", "computed", POLICY, "2026-11"),
        ("FLEET-NOV-DEP", "FLEET", "depreciate", "0", "asset", "200.00", "OPEN-FLEET", "data", POLICY, "2026-11"),
        ("PRE-NOV-AMORT", "PRE", "amortize", "1", "month", "100.00", "OPEN-PRE", "data", POLICY, "2026-11"),
        ("SERVICE-NOV-RELEASE", "SERVICE", "release", "1", "job", "300.00", "SERVICE-OCT-CAP", "data", POLICY, "2026-11"),
    ]
    of = ["id", "asset_id", "quantity", "unit", "amount", "status", "evidence", "period"]
    opening = [
        ("OPEN-FUEL", "FUEL", "100", "L", "1000.00", "data", "approved prior closing FUEL: " + POLICY, START),
        ("OPEN-FLEET", "FLEET", "1", "asset", "12000.00", "data", "approved prior closing FLEET: " + POLICY, START),
        ("OPEN-PRE", "PRE", "12", "month", "1200.00", "data", "approved prepaid balance: " + POLICY, START),
        ("OPEN-SERVICE", "SERVICE", "0", "job", "0.00", "explicit_zero", "approved zero opening: " + POLICY, START),
    ]
    cf = ["id", "goods_id", "op", "quantity", "unit", "ref", "status", "evidence", "period"]
    cr = [
        ("CUST-OPEN", "CUSTOMER-GOODS", "opening", "10", "pallet", "APP-CUST", "data", POLICY, START),
        ("CUST-OCT-IN", "CUSTOMER-GOODS", "receive", "20", "pallet", "", "data", POLICY, START),
        ("CUST-OCT-OUT", "CUSTOMER-GOODS", "dispatch", "10", "pallet", "", "data", POLICY, START),
        ("CUST-NOV-IN", "CUSTOMER-GOODS", "receive", "10", "pallet", "", "data", POLICY, "2026-11"),
        ("CUST-NOV-OUT", "CUSTOMER-GOODS", "dispatch", "5", "pallet", "", "data", POLICY, "2026-11"),
        ("CUST-NOV-RETURN", "CUSTOMER-GOODS", "return", "2", "pallet", "CUST-OCT-OUT", "data", POLICY, "2026-11"),
    ]
    files = {"settlements": table(sf, sr, period), "assets": table(af, ar, period),
             "asset_openings": table(of, opening, period), "custody": table(cf, cr, period)}
    scope = {"book": BOOK, "period": period, "currency": "CNY"}
    cost_fields = ["id", "book", "period", "currency", "kind", "status", "amount", "economic_key", "evidence", "tag", "target_object_id", "origin_period"]
    cost_rows = [{"id": "COST-" + tag, **scope, "kind": "expense", "status": "data", "amount": amount,
                  "economic_key": "COST-" + tag, "evidence": POLICY, "tag": tag,
                  "target_object_id": "service-1", "origin_period": ""} for tag, amount in costs(period).items()]
    files["finance"] = csv_text(cost_fields, cost_rows)
    recognition = {"version": 2, "name": "Financial control cost ledger", "context": scope,
        "rules": [{"id": t, "op": "recognize", "source_tag": t, "source_kind": "expense", "posting_pool": "period"} for t in costs(period)],
        "outputs": list(costs(period)), "excluded_tags": {}, "ledger_basis": "recognized_expenses_incurred",
        "posting_map": {t: {"object_id": "service-1", "object_kind": "service", "record_type": "entry",
                           "cost_element": t, "behavior": "unknown", "cost_type": "direct"} for t in costs(period)}}
    ledger = {"version": 1, "policy_source": POLICY, "history_start": START,
      "settlement": {"file": "settlements", "columns": {k:k for k in sf if k!="period"},
        "accounts": {"AR": {"kind":"receivable","opening_ref":"APP-AR","counterparty":"Synthetic customer","revenue_treatment":"recognized_service","revenue_policy_source":POLICY},
                     "AP": {"kind":"payable","opening_ref":"APP-AP","counterparty":"Synthetic carrier"},
                     "COD": {"kind":"agency","opening_ref":"APP-COD","counterparty":"Synthetic merchant"}},
        "cost_bindings": [{"account_ids":["AP"],"cost_element":"carrier_subcontract","object_ids":["service-1"]}]},
      "assets": {"file":"assets","columns":{k:k for k in af if k!="period"},
        "opening":{"file":"asset_openings","columns":{k:k for k in of if k!="period"}},
        "accounts": {"FUEL":{"kind":"inventory","unit":"L","valuation":"moving_average","policy_source":POLICY},
                     "FLEET":{"kind":"fixed","unit":"asset","valuation":"declared","policy_source":POLICY},
                     "PRE":{"kind":"prepaid","unit":"month","valuation":"declared","policy_source":POLICY},
                     "SERVICE":{"kind":"service_in_progress","unit":"job","valuation":"declared","policy_source":POLICY}},
        "cost_bindings":[{"asset_ids":["FUEL"],"cost_element":"fuel","object_ids":["service-1"]},
                         {"asset_ids":["FLEET"],"cost_element":"depreciation","object_ids":["service-1"]},
                         {"asset_ids":["PRE"],"cost_element":"amortization","object_ids":["service-1"]},
                         {"asset_ids":["SERVICE"],"cost_element":"service_release","object_ids":["service-1"]}]},
      "custody":{"file":"custody","columns":{k:k for k in cf if k!="period"},
        "goods":{"CUSTOMER-GOODS":{"unit":"pallet","opening_ref":"APP-CUST"}}}}
    schema = {"dataset_id":"csv-0123456789abcdef","plugin_id":"csv-generic","plugin_version":"financial-ledgers-test-v1",
              "industry":"Generic completed services with controlled auxiliary ledgers","period":period,"currency":"CNY",
              "raw_composite":{"version":4,"scope":scope,"inputs":{"finance":{"file":"finance","raw_recognition":recognition}},
                 "source_hashes":{n:hashlib.sha256(v.encode()).hexdigest() for n,v in files.items()}},
              "business_views":{"version":1,"policy_source":POLICY,"classifications":{"service-1":"service"},
                 "financial_ledgers":ledger}}
    oracle = {"oracle":"independent declared cents and quantity formulas; no engine import",
              "scope":scope,"cost_groups":costs(period),"total_cost":"1600.00" if period==START else "1762.00",
              "industry":"financial_"+period.replace("-","_"),
              "source_rows":{n:len(list(csv.DictReader(io.StringIO(v)))) for n,v in files.items()},
              "asset_total":"0.00","pnl_cost":"1600.00" if period==START else "1762.00",
              "period_expense":"0.00","service_cost":"1600.00" if period==START else "1762.00",
              "objects":{"service-1":{"total_cost":"1600.00" if period==START else "1762.00"}},
              "asset_opening":"14200.00" if period==START else "14000.00",
              "asset_additions":"300.00" if period==START else "240.00",
              "asset_recognition":"500.00" if period==START else "912.00",
              "asset_closing":"14000.00" if period==START else "13328.00",
              "receivable_closing":"800.00" if period==START else "500.00",
              "payable_closing":"500.00" if period==START else "450.00",
              "agency_closing":"400.00" if period==START else "300.00",
              "custody_quantity":"20" if period==START else "27"}
    return files, schema, oracle


def refresh(files, schema):
    schema["raw_composite"]["source_hashes"]={n:hashlib.sha256(v.encode()).hexdigest() for n,v in files.items()}


def mutate(files, name, record_id, **changes):
    reader=csv.DictReader(io.StringIO(files[name]));fields=reader.fieldnames;rows=list(reader)
    next(row for row in rows if row["id"]==record_id).update(changes)
    files[name]=csv_text(fields,rows)


def analyse(files, schema):
    from cost_core.csv_adapter import CsvCostingAdapter
    from cost_core.costing import CostingEngine
    request=CsvCostingAdapter().from_files(files,schema)
    return CostingEngine().analyze(request)


class FinancialLedgersTests(unittest.TestCase):
    def test_two_period_cost_asset_cash_and_custody_bridge(self):
        for period in (START,"2026-11"):
            with self.subTest(period=period):
                files,schema,oracle=fixture(period)
                out=analyse(files,schema);fin=out["business_views"]["financial_ledgers"]
                self.assertEqual(out["business_views"]["presentation_version"],4)
                self.assertEqual(D(out["totals"]["total_cost"]),D(oracle["total_cost"]))
                self.assertEqual({k:D(v) for k,v in out["totals"]["cost_structure"].items()},
                                 {k:D(v) for k,v in oracle["cost_groups"].items()})
                assets=fin["assets"]["totals"]
                for field,expected in [("opening_value","asset_opening"),("additions","asset_additions"),
                                       ("recognized_cost","asset_recognition"),("closing_value","asset_closing")]:
                    self.assertEqual(D(assets[field]),D(oracle[expected]))
                self.assertEqual(out["business_views"]["asset_total"],"0")
                st=fin["settlement"]["totals"]
                self.assertEqual(st["receivable_closing"],oracle["receivable_closing"])
                self.assertEqual(st["payable_closing"],oracle["payable_closing"])
                self.assertEqual(st["agency_liability_closing"],oracle["agency_closing"])
                self.assertFalse(fin["settlement"]["agency_is_income_or_cost"])
                balances={r["account_id"]:r for r in fin["settlement"]["rows"]}
                self.assertEqual(balances["AR"]["outstanding_debit"],oracle["receivable_closing"])
                self.assertEqual(balances["AR"]["outstanding_credit"],"0.00")
                self.assertEqual(balances["AP"]["outstanding_debit"],"0.00")
                self.assertEqual(balances["AP"]["outstanding_credit"],oracle["payable_closing"])
                self.assertEqual(balances["COD"]["outstanding_credit"],oracle["agency_closing"])
                self.assertEqual(fin["custody"]["rows"][0]["closing_quantity"],oracle["custody_quantity"])
                self.assertFalse(fin["custody"]["monetary_asset_or_cost"])
                self.assertEqual(fin["cost_bridge"]["asset_opening_plus_activity"],
                                 fin["cost_bridge"]["cost_plus_asset_closing"])
                self.assertTrue(out["ledger_reconciled"])
                self.assertTrue(all(x["status"]=="matched" for x in fin["assets"]["cost_reconciliation"]))
                self.assertTrue(any(r["source_uri"]=="upload://sources/assets.csv" for a in fin["assets"]["rows"] for r in a["source_refs"]))
        self.assertEqual(fin["settlement"]["totals"]["declared_net_revenue"],"500.00")
        self.assertEqual(fin["settlement"]["totals"]["net_cash_from_declared_accounts"],"300.00")
        self.assertEqual(fin["assets"]["rows"][-1]["opening_basis"],"computed_prior_period_closing")

    def test_billing_only_does_not_turn_invoice_or_cash_into_revenue(self):
        f,s,_=fixture();ar=s["business_views"]["financial_ledgers"]["settlement"]["accounts"]["AR"]
        ar["revenue_treatment"]="billing_only";ar.pop("revenue_policy_source")
        fin=analyse(f,s)["business_views"]["financial_ledgers"]
        row=next(x for x in fin["settlement"]["rows"] if x["account_id"]=="AR")
        self.assertEqual(row["net_billed"],"500.00");self.assertIsNone(row["net_revenue"])
        self.assertIsNone(fin["settlement"]["totals"]["declared_net_revenue"])
        self.assertEqual(row["cash_in"],"800.00")

    def test_receivable_revenue_policy_is_explicit_not_default(self):
        f,s,_=fixture();s["business_views"]["financial_ledgers"]["settlement"]["accounts"]["AR"].pop("revenue_treatment")
        with self.assertRaisesRegex(ValueError,"AR revenue treatment required"):analyse(f,s)

    def test_crossperiod_capitalize_release_and_inventory_formula(self):
        files,schema,_=fixture();fin=analyse(files,schema)["business_views"]["financial_ledgers"]
        assets={r["asset_id"]:r for r in fin["assets"]["rows"]}
        self.assertEqual(assets["SERVICE"]["opening_value"],"300.00")
        self.assertEqual(assets["SERVICE"]["service_release"],"300.00")
        self.assertEqual(assets["SERVICE"]["closing_value"],"0.00")
        self.assertEqual(assets["FUEL"]["closing_quantity"],"70")
        self.assertEqual(assets["FUEL"]["closing_value"],"728.00")
        used=next(x for x in assets["FUEL"]["current_events"] if x["op"]=="consume")
        self.assertEqual(used["amount"],"312.00")
        self.assertIn("carrying_value",used["valuation_formula"])

    def test_cash_reversal_restores_receivable_not_cost(self):
        f,s,o=fixture()
        reader=csv.DictReader(io.StringIO(f["settlements"]));rows=list(reader);fields=reader.fieldnames
        row=dict(rows[-1]);row.update(id="AR-CASH-REV",account_id="AR",kind="receivable",op="cash_reversal",
                                     amount="50.00",ref="AR-NOV-CASH1")
        rows.append(row);f["settlements"]=csv_text(fields,rows);refresh(f,s)
        out=analyse(f,s);fin=out["business_views"]["financial_ledgers"]
        self.assertEqual(fin["settlement"]["totals"]["receivable_closing"],"550.00")
        self.assertEqual(fin["settlement"]["totals"]["net_cash_from_declared_accounts"],"250.00")
        self.assertEqual(D(out["totals"]["total_cost"]),D(o["total_cost"]))

    def test_paid_invoice_credit_then_refund_is_not_double_cost_or_revenue(self):
        f,s,o=fixture();mutate(f,"settlements","AR-NOV-CASH2",amount="600.00")
        reader=csv.DictReader(io.StringIO(f["settlements"]));rows=list(reader);fields=reader.fieldnames
        row=dict(rows[-1]);row.update(id="AR-PAID-CREDIT",account_id="AR",kind="receivable",op="credit",amount="100.00",ref="AR-NOV-INVOICE")
        rows.append(row);refund=dict(row);refund.update(id="AR-PAID-REFUND",op="refund",ref="AR-NOV-CASH2");rows.append(refund)
        f["settlements"]=csv_text(fields,rows);refresh(f,s);out=analyse(f,s);fin=out["business_views"]["financial_ledgers"]
        self.assertEqual(fin["settlement"]["totals"]["receivable_closing"],"200.00")
        self.assertEqual(fin["settlement"]["totals"]["declared_net_revenue"],"400.00")
        self.assertEqual(fin["settlement"]["totals"]["net_cash_from_declared_accounts"],"500.00")
        self.assertEqual(D(out["totals"]["total_cost"]),D(o["total_cost"]))

    def _reject(self,file,row,pattern,**changes):
        f,s,_=fixture();mutate(f,file,row,**changes);refresh(f,s)
        with self.assertRaisesRegex(ValueError,pattern):analyse(f,s)

    def test_wrong_book_period_and_currency_rejected(self):
        for changes,pattern in [({"book":"OTHER"},"book/currency"),({"period":"2026-12"},"outside declared history"),({"currency":"USD"},"book/currency")]:
            with self.subTest(changes=changes):self._reject("assets","FUEL-NOV-BUY",pattern,**changes)

    def test_aliased_ids_and_original_mismatch_rejected(self):
        self._reject("settlements","AR-NOV-CASH1","duplicate or aliased",id="ar-nov-cash2")
        self._reject("settlements","AR-NOV-CASH1","missing original",ref="AP-OCT-INVOICE")
        self._reject("assets","SERVICE-NOV-RELEASE","missing/mismatched original",ref="OPEN-FLEET")

    def test_overcredit_overpayment_over_cod_and_over_return_rejected(self):
        self._reject("settlements","AR-NOV-CREDIT","excessive credit",amount="1001.00")
        self._reject("settlements","AP-NOV-CASH1","excessive settlement",amount="600.00")
        self._reject("settlements","COD-NOV-REMIT2","excessive agency",amount="600.00")
        self._reject("custody","CUST-NOV-RETURN","excessive custody return",quantity="11")

    def test_over_release_and_missing_capitalization_policy_rejected(self):
        self._reject("assets","SERVICE-NOV-RELEASE","quantity exceeds|release exceeds",quantity="2")
        f,s,_=fixture();s["business_views"]["financial_ledgers"]["assets"]["accounts"]["SERVICE"]["policy_source"]=""
        with self.assertRaisesRegex(ValueError,"invalid asset policy"):analyse(f,s)

    def test_source_tamper_and_missing_opening_rejected(self):
        f,s,_=fixture();f["assets"]=f["assets"].replace("240.00","241.00")
        with self.assertRaisesRegex(ValueError,"hash mismatch"):analyse(f,s)
        self._reject("asset_openings","OPEN-FUEL","required asset.*unavailable",amount="",quantity="",status="missing")
        f,s,_=fixture();s["business_views"]["financial_ledgers"]["assets"]["opening"]["file"]="absent"
        with self.assertRaisesRegex(ValueError,"named file mismatch|missing source"):analyse(f,s)

    def test_asset_cost_cannot_drift_from_engine_or_double_bind(self):
        self._reject("assets","FLEET-NOV-DEP","does not match engine cost",amount="201.00")
        f,s,_=fixture();bindings=s["business_views"]["financial_ledgers"]["assets"]["cost_bindings"]
        bindings.append(copy.deepcopy(bindings[0]))
        with self.assertRaisesRegex(ValueError,"bound more than once"):analyse(f,s)

    def test_wrong_unit_and_valuation_rejected(self):
        self._reject("assets","FUEL-NOV-USE","asset/unit mismatch",unit="pallet")
        self._reject("assets","FUEL-NOV-USE","differs from approved",status="data",amount="315.00")

    def test_missing_receipt_is_unknown_not_zero(self):
        f,s,_=fixture();mutate(f,"settlements","AR-NOV-CASH2",amount="",status="missing");refresh(f,s)
        fin=analyse(f,s)["business_views"]["financial_ledgers"];ar=next(x for x in fin["settlement"]["rows"] if x["account_id"]=="AR")
        self.assertEqual(fin["status"],"incomplete")
        self.assertEqual(ar["status"],"missing");self.assertIsNone(ar["closing"])
        self.assertIsNone(fin["settlement"]["totals"]["net_cash_from_declared_accounts"])

    def test_account_period_absence_requires_evidence_for_zero(self):
        files, schema, _ = fixture()
        reader = csv.DictReader(io.StringIO(files["settlements"]))
        rows = [r for r in reader if not (r["account_id"] == "AR" and r["period"] == "2026-11")]
        files["settlements"] = csv_text(reader.fieldnames, rows)
        refresh(files, schema)
        fin = analyse(files, schema)["business_views"]["financial_ledgers"]
        row = next(r for r in fin["settlement"]["rows"] if r["account_id"] == "AR")
        self.assertEqual(row["status"], "missing")
        self.assertIsNone(row["closing"])
        self.assertIsNone(row["net_revenue"])
        self.assertIsNone(fin["settlement"]["totals"]["cash_in"])

        idle = dict(rows[0], id="AR-NOV-IDLE", op="no_activity", ref="", period="2026-11")
        files["settlements"] = csv_text(reader.fieldnames, rows + [idle])
        refresh(files, schema)
        fin = analyse(files, schema)["business_views"]["financial_ledgers"]
        row = next(r for r in fin["settlement"]["rows"] if r["account_id"] == "AR")
        self.assertEqual(row["status"], "provided")
        self.assertEqual(row["opening"], "800.00")
        self.assertEqual(row["closing"], "800.00")
        self.assertEqual(row["cash_in"], "0.00")
        self.assertEqual(row["net_revenue"], "0.00")
        self.assertTrue(any("AR-NOV-IDLE" in ref["selector"] for ref in row["source_refs"]))

    def test_no_activity_requires_zero_and_no_other_movement(self):
        self._reject("settlements", "AR-OPEN", "explicit_zero cannot carry", amount="1.00")
        self._reject("settlements", "AR-NOV-INVOICE", "no_activity requires explicit zero",
                     op="no_activity", amount="0.00")
        self._reject("settlements", "AR-NOV-INVOICE", "conflicts with no_activity",
                     op="no_activity", amount="0.00", status="explicit_zero")

    def test_custody_and_assets_require_period_activity_evidence(self):
        files, schema, _ = fixture()
        reader = csv.DictReader(io.StringIO(files["custody"]))
        files["custody"] = csv_text(reader.fieldnames, [r for r in reader if r["period"] == START])
        refresh(files, schema)
        custody = analyse(files, schema)["business_views"]["financial_ledgers"]["custody"]
        self.assertEqual(custody["status"], "incomplete")
        self.assertIsNone(custody["rows"][0]["closing_quantity"])

        reader = csv.DictReader(io.StringIO(files["assets"]))
        files["assets"] = csv_text(reader.fieldnames, [r for r in reader if r["id"] != "FLEET-NOV-DEP"])
        refresh(files, schema)
        with self.assertRaisesRegex(ValueError, "FLEET: asset journal missing activity evidence"):
            analyse(files, schema)

    def test_object_scope_does_not_display_book_totals(self):
        f,s,_=fixture()
        from cost_core.csv_adapter import CsvCostingAdapter
        from cost_core.costing import CostingEngine
        out=CostingEngine().analyze(CsvCostingAdapter().from_files(f,s),cost_object="service-1")
        fin=out["business_views"]["financial_ledgers"]
        self.assertEqual(fin["status"],"requires_book_scope");self.assertIsNone(fin["assets"])

    def test_mapped_original_id_and_quantity_columns_keep_real_selectors(self):
        f,s,_=fixture();reader=csv.DictReader(io.StringIO(f["assets"]));rows=list(reader)
        fields=[{"id":"event_no","quantity":"litres"}.get(x,x) for x in reader.fieldnames]
        mapped=[{{"id":"event_no","quantity":"litres"}.get(k,k):v for k,v in row.items()} for row in rows]
        f["assets"]=csv_text(fields,mapped);opt=s["business_views"]["financial_ledgers"]["assets"]
        opt["columns"]["id"]="event_no";opt["columns"]["quantity"]="litres";refresh(f,s)
        fin=analyse(f,s)["business_views"]["financial_ledgers"]
        refs=[r for a in fin["assets"]["rows"] for r in a["source_refs"] if r["source_uri"].endswith("/assets.csv")]
        self.assertTrue(refs);self.assertTrue(all("event_no=" in r["selector"] for r in refs))
        self.assertTrue(any(r["field"]=="litres" for r in refs))

    def test_explicit_zero_keeps_original_file_for_every_following_row(self):
        files, schema, _ = fixture()
        plan = compile_business_views(files, schema)
        result = analyse(files, schema)
        expected_files = {"settlements", "assets", "asset_openings", "custody"}
        seen = set()
        def walk(value):
            if isinstance(value, dict):
                uri = value.get("source_uri", "")
                if uri.startswith("upload://sources/"):
                    name = uri.rsplit("/", 1)[1][:-4]
                    self.assertIn(name, files, uri)
                    if name in expected_files:
                        seen.add(name)
                        query = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(value["selector"])
                        number = int(query["row"][0])
                        record = list(csv.DictReader(io.StringIO(files[name])))[number - 2]
                        self.assertEqual(query["id"][0], record["id"])
                for item in value.values():
                    walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)
        walk(result["business_views"]["financial_ledgers"])
        self.assertEqual(seen, expected_files)

    def test_malformed_contracts_fail_as_validation_errors(self):
        for mutation in (lambda s:s["business_views"]["financial_ledgers"]["assets"]["columns"].update(amount=[]),
                         lambda s:s["business_views"]["financial_ledgers"]["assets"]["accounts"]["FUEL"].update(kind=[]),
                         lambda s:s["business_views"]["financial_ledgers"]["assets"]["cost_bindings"][0].update(asset_ids=[{}])):
            f,s,_=fixture();mutation(s)
            with self.assertRaises(ValueError):analyse(f,s)

    def test_legacy_plan_does_not_gain_financial_output(self):
        f,s,_=fixture();s["business_views"].pop("financial_ledgers")
        f={"finance":f["finance"]};refresh(f,s)
        out=analyse(f,s)
        self.assertEqual(out["business_views"]["presentation_version"],2)
        self.assertNotIn("financial_ledgers",out["business_views"])


if __name__=="__main__":
    if "--write-fixtures" in sys.argv:
        destination=Path(sys.argv[sys.argv.index("--write-fixtures")+1]);destination.mkdir(parents=True,exist_ok=True)
        for period in (START,"2026-11"):
            files,schema,expected=fixture(period);folder=destination/("financial_"+period.replace("-","_"));folder.mkdir(exist_ok=True)
            for name,text in files.items():(folder/(name+".csv")).write_bytes(text.encode("utf-8"))
            (folder/"rules.json").write_bytes(json.dumps(schema,ensure_ascii=False,indent=2).encode())
            (folder/"expected.json").write_bytes(json.dumps(expected,ensure_ascii=False,indent=2).encode())
        print("INDEPENDENT_FIXTURES_WRITTEN",destination)
    else:unittest.main()