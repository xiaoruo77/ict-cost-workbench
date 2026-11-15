import csv,io,unittest
from decimal import Decimal as D
from cost_core.costing import CostingEngine
from cost_core.csv_adapter import CsvCostingAdapter

def fixture():
    rows=[]; cats={}; rules=[]; sources={}; controls={}; b={}
    q={"A":100,"B":200,"C":300}
    sc=dict(fleet="owned",vendor="outsourced",station="site",office="overhead",A="ownroute",B="ownroute",C="outsourceroute")
    ps={v:{} for v in set(sc.values())}
    def put(k,g,system,owner,m,doc,weights,driver,split=""):
        cats[g]=dict(label=g,kind="group")
        cats[k]=dict(label=k,kind="posting",parent=g,source=system,allocation="pool" if weights else "entry")
        sources[system]=dict(state="reviewed",evidence=system+" independent simulated register")
        ps[sc[owner]][k]="required"
        controls[system,doc]=controls.get((system,doc),0)+m;b[k]=b.get(k,0)+m
        pid=doc+split if weights else ""
        rows.append(dict(object_id=owner,object_kind="service" if owner in q else "cost_center",currency="CNY",
            quantity=q.get(owner),quantity_unit="orders",record_type="pool" if weights else "entry",
            cost_element=k,amount=m,pool_id=pid,source_system=system,document_id=doc,line_id="1",split_id=split))
        if weights:rules.append(dict(pool_id=pid,target_ids=list(weights),weights=weights,driver_name=driver,driver_unit=driver))
    for split in "ab":put("fuel","transport","fuel_card","fleet",6000,"fu01",{"A":1,"B":2},"kg-km",split)
    for k,g,ss,v,a,z,driver in [
        ("toll","transport","road",6000,1,2,"toll segments"),
        ("payroll","transport","payroll",14000,70,70,"driver hours"),
        ("depreciation","fleet_site","assets",9000,10,20,"operated trips"),
        ("repairs","fleet_site","fleet",3000,1,2,"vehicle mileage"),
        ("tires","fleet_site","fleet",1500,1,2,"vehicle mileage"),
        ("insurance","fleet_site","ins policies",1800,3,3,"insured vehicles")]:
        put(k,g,ss,"fleet",v,"rp01" if k=="repairs" else k+"01",{"A":a,"B":z},driver)
    put("carrier","transport","vendor invoice","vendor",24000,"ca01",{"C":1},"contract trips")
    put("handling","fleet_site","site invoices","station",2400,"ha01",q,"orders")
    put("warehouse","fleet_site","site invoices","station",3600,"wh01",q,"orders")
    put("admin","overhead","accounting","office",6000,"ad01",q,"orders")
    put("loss","exception","claims","B",900,"lo01",None,"")
    put("recovery","exception","claims","B",-200,"re01",None,"")
    cats["empty_fuel"]=dict(label="空驶燃油",kind="analysis",parent="fuel")
    cats["buyer_toll"]=dict(label="买方另付通行费",kind="posting",parent="transport",source="road",allocation="entry")
    ps["outsourceroute"].update(loss="required",recovery="required",buyer_toll="required")
    post=sorted(k for k,v in cats.items() if v["kind"]=="posting")
    for p in ps.values():
        for cat in post:p.setdefault(cat,"na:according to the declared ownership and contracts")
    req={k:sorted({cats[cat]["source"] for cat,x in p.items() if x=="required"}) for k,p in ps.items()}
    headers=list(rows[0])
    for k in q:rows.append(dict(object_id=k,object_kind="service",currency="CNY",quantity=q[k],quantity_unit="orders",record_type="object"))
    z=lambda k:dict(state="zero",evidence="verified synthetic close register "+k)
    budget={k:dict(budget=b.get(k,0),evidence="synthetic October budget") for k in post}
    budget["fuel"].update(budget=10800,actual_quantity=1600,actual_rate="7.5",budget_quantity=1500,budget_rate="7.2")
    budget["carrier"].update(budget=21840,actual_quantity=30,actual_rate=800,budget_quantity=28,budget_rate=780)
    c=dict(version="1",period="2026-10",categories=cats,scopes=sc,policies=ps,required_sources=req,register=sources,
         controls=[dict(source_system=ss,document_id=d,line_id="1",amount=a) for (ss,d),a in controls.items()],
         observations={"A":{k:z(k) for k in ("loss","recovery")},"C":{k:z(k) for k in ("loss","recovery","buyer_toll")}},
         analysis={"fleet":{"empty_fuel":dict(amount=1800,evidence="subset of fuel parent invoice")}},comparisons=budget)
    return rows,headers,dict(period="2026-10",completeness=c,allocation_rules=rules,
            benchmarks=[dict(object_id=k,scenario="budget",total_cost=v,currency="CNY") for k,v in dict(A=20000,B=32800,C=27840).items()])

def csv_text(rows,headers):
    f=io.StringIO();w=csv.DictWriter(f,fieldnames=headers);w.writeheader();w.writerows(rows);return f.getvalue()

def run(rows,headers,schema):
    return CostingEngine().analyze(CsvCostingAdapter().from_text(csv_text(rows,headers),schema))

class Coverage(unittest.TestCase):
    def test_reconciles(self):
        r=run(*fixture());c=r["coverage"]
        self.assertEqual(c["status"],"complete_for_declared_scope",c["issues"])
        self.assertEqual({D(c[k]) for k in ("ledger_total","mapped_total","controls_total","allocated_or_retained_total")},{84000})
        self.assertEqual(D(c["unallocated_total"]),0)
        self.assertEqual(sum(D(x["amount"]) for x in c["category_totals"].values() if x["kind"]=="group"),84000)
        self.assertEqual(D(c["category_totals"]["fuel"]["amount"]),12000)
        self.assertEqual(D(c["analytical"][0]["amount"]),1800)
        self.assertNotIn("empty_fuel",c["category_totals"])
        cells={(x["scope"],x["category"]):x["state"] for x in c["cells"]}
        self.assertEqual(cells[("vendor","fuel")],"not_applicable")
        self.assertEqual(cells[("C","buyer_toll")],"verified_zero")
        self.assertEqual({x["object_id"]:tuple(map(D,(x["total_cost"],x["unit_cost"]))) for x in r["objects"] if x["object_id"] in "ABC"},
            {"A":(20400,204),"B":(33600,168),"C":(30000,100)})
        self.assertEqual({x["object_id"]:D(x["variance"]) for x in r["benchmarks"]},{"A":400,"B":800,"C":2160})
        self.assertEqual(sum(D(x["budget"]) for x in c["comparisons"]),80640)
        self.assertEqual(sum(D(x["variance"]) for x in c["comparisons"]),3360)
        self.assertEqual({x["category"]:(D(x["usage_effect"]),D(x["rate_effect"])) for x in c["comparisons"] if "rate_effect" in x},
                         {"fuel":(720,480),"carrier":(1560,600)})
        self.assertTrue(all(x["source_refs"] and x["formula"] for x in r["allocations"]))

    def test_incomplete_and_identity_guards(self):
        rows,heads,s=fixture()
        s["completeness"]["register"]["road"]["state"]="missing"
        rows=[r for r in rows if r.get("cost_element")!="repairs"]
        s["allocation_rules"]=[r for r in s["allocation_rules"] if r["pool_id"]!="rp01"]
        c=run(rows,heads,s)["coverage"]
        self.assertEqual(c["status"],"incomplete")
        codes={x["code"] for x in c["issues"]}
        self.assertTrue({"coverage_source_not_reviewed","coverage_zero_source_not_reviewed","coverage_required_missing","coverage_expected_posting_missing"}<=codes)
        rows,heads,s=fixture();x=next(r for r in rows if r.get("cost_element")=="loss");x["cost_element"]="unmapped"
        self.assertEqual(D(run(rows,heads,s)["coverage"]["unmapped_total"]),900)
        rows,heads,s=fixture();s["completeness"]["policies"]["ownroute"]["loss"]="na:fixture conflict"
        del s["completeness"]["observations"]["A"]["loss"]
        self.assertIn("coverage_not_applicable_booked",{v["code"] for v in run(rows,heads,s)["coverage"]["issues"]})
        rows,heads,s=fixture();rows.append({**rows[0],"pool_id":"new-pool"})
        with self.assertRaisesRegex(ValueError,"duplicate source posting"):run(rows,heads,s)
        rows,heads,s=fixture();s["completeness"]["controls"][0]["amount"]=12001
        self.assertIn("coverage_control_difference",{v["code"] for v in run(rows,heads,s)["coverage"]["issues"]})
        rows,heads,s=fixture();s["completeness"]["observations"]["A"]["loss"]["evidence"]=""
        with self.assertRaisesRegex(ValueError,"zero evidence"):run(rows,heads,s)
        rows,heads,s=fixture();s["allocation_rules"]=[v for v in s["allocation_rules"] if v["pool_id"]!="rp01"]
        self.assertEqual(D(run(rows,heads,s)["coverage"]["unallocated_total"]),3000)

    def test_other_industry_new_config_only(self):
        h="object_id currency record_type cost_element amount source_system document_id line_id".split()
        rows=[dict(zip(h,["service","CNY","entry","cloud",750,"billing","A1","01"]))]
        plugin=dict(version="1",period="2026-10",scopes={"service":"saas"},
            categories={"cloud":dict(label="云资源",kind="posting",source="billing",allocation="entry"),
                        "inventory":dict(label="库房",kind="posting",source="warehouse",allocation="entry")},
            policies={"saas":{"cloud":"required","inventory":"na:纯软件订阅"}},
            required_sources={"saas":["billing"]},
            register={"billing":dict(state="reviewed",evidence="模拟月账"),"warehouse":dict(state="missing",evidence="范围外")},
            controls=[dict(source_system="billing",document_id="A1",line_id="01",amount=750)])
        r=run(rows,h,dict(period="2026-10",completeness=plugin))
        self.assertEqual(r["coverage"]["status"],"complete_for_declared_scope")
        self.assertNotIn("coverage",run(rows,h,dict(period="2026-10")))

    def test_period_or_net_zero_cannot_fake_complete(self):
        rows,h,s=fixture();s["completeness"]["period"]="2026-09"
        r=run(rows,h,s)
        self.assertTrue(r["ledger_reconciled"])
        self.assertIn("coverage_control_period_mismatch",{i["code"] for i in r["coverage"]["issues"]})
        rows,h,s=fixture();del s["completeness"]["period"]
        self.assertEqual(run(rows,h,s)["coverage"]["status"],"incomplete")
        rows,h,s=fixture()
        for x in rows:
            if x.get("cost_element") in ("loss","recovery"):
                if x["cost_element"]=="loss": x["amount"]=200
                x.update(record_type="pool",pool_id=x["document_id"],object_id="office",object_kind="cost_center",quantity=None)
                cat=x["cost_element"]
                s["completeness"]["categories"][cat]["allocation"]="pool"
                s["completeness"]["policies"]["overhead"][cat]="required"
                s["completeness"]["observations"].setdefault("B",{})[cat]=dict(state="zero",evidence="negative fixture")
                if "claims" not in s["completeness"]["required_sources"]["overhead"]:
                    s["completeness"]["required_sources"]["overhead"].append("claims")
                s["allocation_rules"].append(dict(pool_id=x["pool_id"],target_ids=["A"],weights={"A":0},allow_partial=True))
        for x in s["completeness"]["controls"]:
            if x["document_id"]=="lo01":x["amount"]=200
        r=run(rows,h,s);c=r["coverage"]
        self.assertTrue(r["ledger_reconciled"]);self.assertEqual(D(c["unallocated_total"]),0)
        self.assertEqual(c["status"],"incomplete")
        self.assertEqual(len(next(x for x in c["issues"] if x["code"]=="coverage_unallocated_amount")["unresolved"]),2)
