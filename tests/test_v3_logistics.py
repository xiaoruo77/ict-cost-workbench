import copy,csv,io,random,unittest
from decimal import Decimal as D
from fractions import Fraction
from cost_core.costing import CostingEngine
from cost_core.csv_adapter import CsvCostingAdapter

def fixture(trips, budgets):
    out=io.StringIO()
    w=csv.writer(out)
    w.writerow("rid object_id object_kind currency record_type cost_element amount pool_id".split())
    rules=[]
    for trip, amount, items in trips:
        w.writerow([trip,trip,"cost_center","CNY","pool","transport",amount,trip])
        rules.append(dict(pool_id=trip,target_ids=list(items),driver_name="payload distance",
             driver_unit="kg·km",weight_factors=dict(columns=["kg","km"],values={
                order:dict(zip(("kg","km"),values)) for order,values in items.items()})))
    for order in budgets:
        w.writerow([order,order,"service","CNY","entry","transport",0,""])
    return out.getvalue(),dict(period="2026-11",row_identity=["rid"],allocation_rules=rules,
         benchmarks=[dict(object_id=k,scenario="budget",total_cost=v,currency="CNY") for k,v in budgets.items()])

class Logistics(unittest.TestCase):
    def analyze(self, data, schema):
        return CostingEngine().analyze(CsvCostingAdapter().from_text(data,schema))

    def test_fixed(self):
        data,schema=fixture([("T1",1000,{"A":(100,10),"B":(200,20)}),
                             ("T2",600,{"A":(100,30),"C":(100,10)})],{"A":600,"B":850,"C":100})
        r=self.analyze(data,schema)
        self.assertEqual({(a["pool_id"],a["to_object"]):D(a["amount"]) for a in r["allocations"]},
                         {("T1","A"):200,("T1","B"):800,("T2","A"):450,("T2","C"):150})
        self.assertEqual({x["object_id"]:D(x["variance"]) for x in r["benchmarks"]},{"A":50,"B":-50,"C":50})
        self.assertEqual(D(r["totals"]["total_cost"]),1600)
        self.assertTrue(all(a["formula"] and a["source_refs"] for a in r["allocations"]))
        print("fixed_actual=1600 fixed_budget=1550 variance=50")

    def test_seeded_rounding(self):
        rng=random.Random(90125)
        trips=[]; budgets={f"O{i:03}":i+50 for i in range(180)}
        for i in range(193):
            ids=rng.sample(list(budgets),9)
            trips.append((f"T{i:03}",str(D(rng.randrange(20_000,145_000))/100),
                          {k:(rng.randrange(1,121),rng.randrange(1,311)) for k in ids}))
        r=self.analyze(*fixture(trips,budgets))
        observed={(x["pool_id"],x["to_object"]):int(D(x["amount"])*100) for x in r["allocations"]}
        expected={}
        for trip,cost,items in trips:
            cents=int(D(cost)*100)
            weights={k:x*y for k,(x,y) in items.items()}
            ratios={k:Fraction(cents*w,sum(weights.values())) for k,w in weights.items()}
            floors={k:int(q) for k,q in ratios.items()}
            for k in sorted(ratios,key=lambda k:(-(ratios[k]-floors[k]),k))[:cents-sum(floors.values())]:
                floors[k]+=1
            expected.update({(trip,k):v for k,v in floors.items()})
        self.assertEqual(observed,expected)
        self.assertEqual(sum(observed.values()),int(sum(D(t[1]) for t in trips)*100))
        actual={k:sum(v for (trip,target),v in expected.items() if target==k) for k in budgets}
        self.assertEqual({x["object_id"]:int(D(x["variance"])*100) for x in r["benchmarks"]},
                         {k:actual[k]-100*v for k,v in budgets.items()})
        print("seed=90125 trips=193 orders=180 links=1737 cents_total="+str(sum(observed.values())))

    def test_validation(self):
        data,s=fixture([("T",10,{"A":(1,1),"B":(1,1)})],{"A":1,"B":2})
        for values in ({"A":{"kg":1,"km":1}},{"A":{"kg":1,"km":-1},"B":{"kg":1,"km":1}},
            {"A":{"kg":0,"km":1},"B":{"kg":1,"km":0}},{"A":{"kg":1},"B":{"kg":1,"km":1}}):
            bad=copy.deepcopy(s);bad["allocation_rules"][0]["weight_factors"]["values"]=values
            with self.assertRaises(ValueError):self.analyze(data,bad)
        repeated=data+data.splitlines()[-1]+"\n"
        with self.assertRaisesRegex(ValueError,"duplicates"):self.analyze(repeated,s)
        del s["row_identity"]
        self.analyze(repeated,s)
        s["allocation_rules"][0]["weight_factors"]["values"]["A"]["kg"]=0
        self.assertEqual(sum(D(x["amount"]) for x in self.analyze(data,s)["allocations"]),10)
