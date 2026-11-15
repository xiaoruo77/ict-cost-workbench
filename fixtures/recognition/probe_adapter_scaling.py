"""Run against an actual cloud candidate via PYTHONPATH. Does not modify it."""
import argparse, csv, io, time
from decimal import Decimal as D
from urllib.parse import parse_qsl
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine

PERIOD = "2026-11"
KEYS = ("id","book","period","currency","kind","status","amount","economic_key","evidence","tag")
BOUNDS = [1, 64, 65, 100, 500, 501, 5844]

def make_rows(n, offsets=False, duplicate=False):
    values = [(f"power-{i:05d}", "2", "expense", "power") for i in range(n)]
    if offsets:
        values += [("power-adjustment", "12.34", "expense", "power"),
                   ("power-credit", "-12.34", "credit", "credit")]
    if duplicate:
        values[-1] = (values[0][0], *values[-1][1:])
    return values

def config(offsets=False):
    pool=dict(object_id="site",object_kind="cost_center",record_type="pool",cost_element="energy",
              behavior="fixed",cost_type="indirect",pool_id="electricity")
    cfg=dict(version=2,name="Utilities",context=dict(book="test-ledger",period="from_schema",currency="CNY"),
             rules=[dict(id="power",op="recognize",source_tag="power",source_kind="expense",posting_pool="energy")],
             outputs=["power"],excluded_tags={},posting_map=dict(power=pool),
             ledger_basis="recognized_expenses_incurred",
             target_objects=[dict(object_id=o,object_kind="service") for o in ("A","B")])
    if offsets:
        cfg["rules"] += [dict(id="credit",op="recognize",source_tag="credit",source_kind="credit",posting_pool="energy")]
        cfg["rules"] += [dict(id="net",op="sum",inputs=["power","credit"])]
        cfg["outputs"] += ["credit","net"]
        cfg["posting_map"]["credit"] = pool.copy()
    return dict(period=PERIOD,allocation_rules=[dict(pool_id="electricity",target_ids=["A","B"],weights={"A":3,"B":1})],
                raw_recognition=cfg)

def csv_bytes(rows):
    s=io.StringIO(newline="")
    w=csv.DictWriter(s,fieldnames=KEYS)
    w.writeheader()
    for n,a,k,t in rows:
        w.writerow(dict(id=n,book="test-ledger",period=PERIOD,currency="CNY",kind=k,status="data",amount=a,
                        economic_key=n if t!="credit" else f"independent-key-{n}",evidence=f"synthetic-{n}",tag=t))
    return s.getvalue()

def verify(n, offsets=False, reverse=False):
    expected=D(n)*2
    rows=make_rows(n,offsets)
    if reverse:
        rows.reverse()
    orig={r[0]:i for i,r in enumerate(rows,2)}
    t=time.perf_counter()
    req=CsvCostingAdapter().from_text(csv_bytes(rows),config(offsets),source_uri="upload://data.csv")
    assert len(req.pools)==1, len(req.pools)
    p=req.pools[0]
    assert p.amount==expected,(p.amount,expected)
    got={}
    for ref in p.source_refs:
        e=dict(parse_qsl(ref.selector))
        assert e["id"] not in got
        assert ref.source_uri=="upload://data.csv"
        got[e["id"]]=int(e["row"])
    assert len(got)==len(orig) and got==orig, (len(got),len(orig))
    d=CostingEngine().analyze(req)
    objects={o["object_id"]:D(o["total_cost"]) for o in d["objects"]}
    a=expected*D("0.75");b=expected-a
    assert objects["A"]==a and objects["B"]==b,objects
    assert D(d["totals"]["unallocated_cost"])==0,d["totals"]
    assert D(d["totals"]["total_cost"])==expected,d["totals"]
    assert d["ledger_reconciled"] and not d["recognition"]["can_claim_complete"],(d["ledger_reconciled"],d["recognition"])
    if offsets:
        values=d["recognition"]["outputs"]
        assert D(values["power"])==expected+D("12.34"),values
        assert D(values["credit"])==D("-12.34"),values
        assert D(values["net"])==expected,values
    print(f"PASS {n=} {offsets=} {reverse=} amount={expected} refs={len(got)} A={a} B={b} elapsed={time.perf_counter()-t:.3f}s")

def verify_duplicate():
    rows=make_rows(3,offsets=True,duplicate=True)
    try:
        CsvCostingAdapter().from_text(csv_bytes(rows),config(True))
    except ValueError as e:
        assert "duplicate reference" in str(e).lower(),str(e)
        print(f"PASS same ID across different tags/kinds rejected: {e}")
    else:
        raise AssertionError("duplicate evidence ID across categories silently accepted")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--strict",action="store_true",help="return failure status if any expected success fails")
    p.add_argument("--base-only",action="store_true")
    args=p.parse_args()
    scenarios=[(n,False,False) for n in BOUNDS]
    if not args.base_only:
        scenarios.extend([(500,True,False),(5844,True,True)])
    failures=0
    for args_ in scenarios:
        try: verify(*args_)
        except (ValueError,AssertionError) as e:
            failures += 1
            print(f"FAIL n={args_[0]} offsets={args_[1]} reverse={args_[2]}: {e!s}")
    verify_duplicate()
    return int(failures>0 and args.strict)
if __name__=="__main__":
    raise SystemExit(main())
