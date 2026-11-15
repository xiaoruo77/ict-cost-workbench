import csv
import io
import json
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qsl

from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot,load_snapshot,export_snapshot

BASE=dict(version=1,currency="CNY",unit="request",tier_scope="meter",usage_scale=0,currency_scale=2,
          tiers=[{"until":"100000","rate":"0.003"},{"until":None,"rate":"0.007"}],basis_months=0)
def config(profile=BASE, account="A"):
    spec=dict(object_id="service",object_kind="service",cost_type="direct",behavior="variable")
    return {"profile":profile,"scope":dict(book="subscription",period="from_schema",currency="CNY",billing_account=account),
            "posting_map":{name:{**spec,"cost_element":name} for name in ("metered_usage","fixed_fee","vendor_credit")}}

def cols(op, period="2026-11", account=None):
    return {**op["scope"],"period":period,**({"billing_account":account} if account else {})}

def line(op,q,id="a",m="meter-a",period="2026-11",account=None):
    return dict(cols(op,period,account),kind="usage",id=id,meter=m,unit=op["profile"]["unit"],qty=str(q),evidence="log:"+id)

def input(op,rows,period="2026-11"):
    obj=io.StringIO()
    w=csv.DictWriter(obj,fieldnames=sorted({k for row in rows for k in row}))
    w.writeheader();w.writerows(rows)
    return obj.getvalue(),{"raw_meter":op,"period":period,"dataset_id":"csv-123456789abcdef0"}

def evaluate(op,rows,period="2026-11"):
    txt,schema=input(op,rows,period)
    req=CsvCostingAdapter().from_text(txt,schema)
    return req,CostingEngine().analyze(req)

class MeterAdapter(unittest.TestCase):
    def test_thresholds_same_profile_and_cross_scope(self):
        fixed=config()
        for q, amount in ((99999,"300.00"),(100000,"300.00"),(100001,"300.01")):
            with self.subTest(q=q):
                _,out=evaluate(fixed,[line(fixed,q)])
                self.assertEqual(D(out["totals"]["total_cost"]),D(amount))
                self.assertTrue(out["ledger_reconciled"])
                self.assertFalse(out["recognition"]["can_claim_complete"])
        _,out=evaluate(fixed,[line(fixed,150000,period="2026-12")],"2026-12")
        self.assertEqual(D(out["totals"]["total_cost"]),D("650.00"))
        with self.assertRaisesRegex(ValueError,"mixed book"):
            evaluate(fixed,[line(fixed,150000,account="B")])
        other=config(account="B")
        _,out=evaluate(other,[line(other,150000)])
        self.assertEqual(D(out["totals"]["total_cost"]),D("650.00"))

    def test_real_ledger_named_meter_policy_and_fixed_residual(self):
        op=config({**BASE,"basis_months":12})
        scope=cols(op)
        usage=[line(op,75000,"usage-1"),line(op,75000,"usage-2",m="meter-b")]
        basis=dict(scope,id="prepaid",kind="basis",amount="1200.01",origin_period="2026-01",evidence="annual")
        credit=dict(scope,id="credit",kind="vendor_credit",amount="-25",evidence="credit")
        req,res=evaluate(op,usage+[basis,credit])
        self.assertEqual([x.amount for x in req.entries],list(map(D,("450.00","100.00","-25.00"))))
        self.assertEqual(D(res["totals"]["total_cost"]),525)
        self.assertEqual([dict(parse_qsl(x.selector))["row"] for x in req.entries[0].source_refs],["2","3"])
        _,res=evaluate({**op,"profile":{**op["profile"],"tier_scope":"billing_account"}},usage+[basis,credit])
        self.assertEqual(D(res["totals"]["total_cost"]),725)
        def dec(row): return {**row,"period":"2026-12"}
        _,res=evaluate(op,list(map(dec,usage+[basis,credit])),"2026-12")
        self.assertEqual(D(res["totals"]["total_cost"]),D("525.01"))

    def test_utility_volume_scale_and_good_bad_records(self):
        op=config({**BASE,"unit":"m³","usage_scale":3,"tiers":[dict(until=None,rate="1.234")]})
        req,out=evaluate(op,[line(op,"2.125")])
        self.assertEqual(req.entries[0].amount,D("2.62"))
        self.assertEqual(D(out["recognition"]["meter_billing"]["raw_variable"]),D("2.62225"))
        m=line(op,"0")
        _,out=evaluate(op,[m])
        self.assertEqual(D(out["totals"]["total_cost"]),0)
        rejects=([dict(m,evidence="")],[dict(m,unit="EA")],[dict(m,qty="-1")],
                 [dict(m,kind="background_shell")],[m,dict(m,meter="other")])
        for rows in rejects:
            with self.subTest(rows=rows), self.assertRaises(ValueError): evaluate(op,rows)

    def test_same_revision_and_real_qty_evidence(self):
        op=config()
        text,schema=input(op,[line(op,150000)])
        request=CsvCostingAdapter().from_text(text,schema)
        result=CostingEngine().analyze(request)
        with TemporaryDirectory() as t:
            p=Path(t);dest=p/"costing_datasets"/schema["dataset_id"];dest.mkdir(parents=True)
            (dest/"data.csv").write_text(text)
            (dest/"manifest.json").write_text(json.dumps({"schema":schema}))
            first=create_snapshot(result,p,p,dict(dataset_id=schema["dataset_id"],dataset_label="合成",
                                  period="2026-11",scenario="actual",owner_external_id="qa"))
            snap=load_snapshot(first["analysis_id"],p)
            self.assertEqual(snap["revision"],first["revision"])
            tariffs=[r for r in snap["evidence"] if r["selector"]=="raw_meter.profile"]
            self.assertEqual(tariffs[0]["mapping"]["configuration"],BASE)
            refs=[r for r in snap["evidence"] if r["selector"]=="row=2&id=a"]
            self.assertEqual(refs[0]["rows"][0]["value"],"150000")
            md=export_snapshot(snap,"md",p/"export").read_text()
            for phrase in ("计量分档只按申报用量","meter-a","650.00","月净额最后统一舍入"):
                self.assertIn(phrase,md)

if __name__=="__main__": unittest.main()
