import csv,io,json,random,copy
from pathlib import Path
from collections import defaultdict
from fractions import Fraction
from decimal import Decimal as D
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from cost_core.industry_catalog import industry
SEED=723120261114
PERIODS=("2026-09","2026-10")
H="object_id object_kind name currency month qty price client team contract record_type cost_element amount pool_id source_system document_id line_id split_id".split()
def cash(cents):return str(D(cents)/100)
def cents(value):return int(D(value)*100)
def csvtext(rows):
 f=io.StringIO();w=csv.DictWriter(f,fieldnames=H);w.writeheader();w.writerows(rows);return f.getvalue()
def run(rows,s):
 return CostingEngine().analyze(CsvCostingAdapter().from_text(csvtext(rows),s))
def build(month, large=False):
 rng=random.Random(SEED+int(month[-2:]));rows=[];rules=[];controls=[];scopes={};checks={};poollist={};meta={}
 cats={
  "salary":dict(label="交付人员工资",kind="posting",source="payroll",allocation="pool"),
  "seats":dict(label="文档软件席位",kind="posting",source="subscriptions",allocation="pool"),
  "travel":dict(label="已核实差旅",kind="posting",source="expenses",allocation="entry"),
  "vendor_credit":dict(label="供应商返还冲减",kind="posting",source="credit_notes",allocation="entry")}
 policies={
  "team":dict(salary="required",seats="required",travel="na:不在团队费用中心重复入账",vendor_credit="na:直接归合同"),
  "contract":dict(salary="na:由团队动因分摊",seats="na:由席位动因分摊",travel="required",vendor_credit="required")}
 register={v["source"]:dict(state="reviewed",evidence="仿真期间已复核:"+month) for v in cats.values()}
 req={k:[cats[c]["source"] for c,v in ruleset.items() if v=="required"] for k,ruleset in policies.items()}
 obs={}
 def seed_row(oid,category,amt,pool="",piece="",qty=None,price=None,doc=""):
  ref=cats[category]["source"];m=meta.get(oid,("","",""))
  rows.append(dict(object_id=oid,object_kind="cost_center" if pool else "service",name=oid,currency="CNY",
   month=month,qty=qty,price=price,client=m[0],team=m[1],contract=m[2],record_type="pool" if pool else "entry",
   cost_element=category,amount=cash(amt),pool_id=pool,source_system=ref,document_id=doc,line_id="1",split_id=piece))
 def add_control(category,doc,s):
  controls.append(dict(source_system=cats[category]["source"],document_id=doc,line_id="1",amount=cash(s)))
 zero=lambda label:dict(state="zero",evidence="已核对无发生的合成凭证："+label)
 benches=[]
 for i in range(8 if large else 1):
  center=f"team-{i:02}";scopes[center]="team";targets=[];hours={};seats={}
  salary=rng.randrange(725000,8500000) if large else 60000 if month.endswith("09") else 48000
  licenses=rng.randrange(16000,320000) if large else 12000 if month.endswith("09") else 9000
  for j in range(20 if large else 2):
   client=f"customer-{(j*3+i)%27:02}";agreement=f"agreement-{i:02}-{j:02}";oid=f"{center}/{agreement}/{client}" if large else ("C" if not j else "D")
   meta[oid]=(client,center,agreement);targets.append(oid);scopes[oid]="contract";obs[oid]={}
   hours[oid]=rng.randrange(1,72) if large else [5,1][j] if month.endswith("09") else [1,3][j]
   seats[oid]=rng.randrange(1,7) if large else 1 if month.endswith("09") else [2,1][j]
   qty=rng.randrange(5,200) if large else [8,5][j];price=rng.randrange(2000,45000) if large else [15000,16000][j]
   travel=rng.randrange(1000,85000) if large else [9000,3000][j] if month.endswith("09") else [0,7000][j]
   refund=-rng.randrange(65,850) if large and j%4==0 else 0 if large else [-6000,0][j] if month.endswith("09") else [0,-1000][j]
   for cat,a in (("travel",travel),("vendor_credit",refund)):
    doc=cat+"_"+oid
    if not a:obs[oid][cat]=zero(cat);continue
    pieces=3 if large and cat=="travel" else 1
    for n in range(pieces):
     val=a//pieces if n+1<pieces else a-(a//pieces)*(pieces-1)
     seed_row(oid,cat,val,piece=str(n+1),qty=qty,price=cash(price),doc=doc)
    add_control(cat,doc,a)
   rows.append(dict(object_id=oid,object_kind="service",name=oid,currency="CNY",month=month,
       qty=qty,price=cash(price),client=client,team=center,contract=agreement,record_type="object"))
   checks[oid]=dict(travel=travel,vendor_credit=refund,qty=qty,revenue=qty*price)
   benches.append(dict(object_id=oid,scenario="budget",total_cost=cash(rng.randrange(35000,220000) if large else [53000,21000][j] if month.endswith("09") else [20000,43000][j]),currency="CNY"))
  for k,a,w,driver,unit in (("salary",salary,hours,"合同工时","小时"),("seats",licenses,seats,"授权席位","个")):
   doc=f"{center}_{k}"
   seed_row(center,k,a,pool=doc,doc=doc);add_control(k,doc,a)
   rules.append(dict(pool_id=doc,target_ids=targets,weights=w,driver_name=driver,driver_unit=unit))
   poollist[doc]=(k,a,w)
 return rows,dict(industry=industry("7231")["name"],period=month,currency="CNY",columns=dict(object_name="name",period="month",quantity="qty",selling_price="price"),
    allocation_rules=rules,benchmarks=benches,completeness=dict(version="1",period=month,categories=cats,scopes=scopes,register=register,
    policies=policies,required_sources=req,controls=controls,observations=obs)),checks,poollist,meta

def apportion(total,w):
 quot={x:Fraction(total*a,sum(w.values())) for x,a in w.items()};now={x:int(v) for x,v in quot.items()}
 for x in sorted(quot,key=lambda x:(-(quot[x]-now[x]),x))[:total-sum(now.values())]:now[x]+=1
 return now

def confirm(data):
 rows,s,want,pools,meta=data
 r=run(rows,s); c=r["coverage"];assert c["status"]=="complete_for_declared_scope",c["issues"]
 seen={}
 for pid,(cat,total,w) in pools.items():
  for x,a in apportion(total,w).items():want[x][cat]=a;seen[pid,x]=a
 assert {(a["pool_id"],a["to_object"]):cents(a["amount"]) for a in r["allocations"]}==seen
 costs={k:sum(v[name] for name in ("salary","seats","travel","vendor_credit")) for k,v in want.items()}
 for o in r["objects"]:
  key=o["object_id"]
  if key not in costs:continue
  assert cents(o["total_cost"])==costs[key] and cents(o["revenue"])==want[key]["revenue"]
  for cname in ("salary","seats","travel","vendor_credit"):assert cents(o["cost_structure"].get(cname,"0"))==want[key][cname]
  unit=(D(costs[key])/100/want[key]["qty"]).quantize(D("0.0001"))
  assert D(o["unit_cost"]).quantize(D("0.0001"))==unit
 budget={x["object_id"]:cents(x["total_cost"]) for x in s["benchmarks"]};total=sum(costs.values())
 assert cents(r["totals"]["total_cost"])==total==cents(c["controls_total"])
 assert sum(cents(x["amount"]) for x in rows if "amount" in x)==total and not D(c["unallocated_total"])
 assert {x["object_id"]:cents(x["variance"]) for x in r["benchmarks"]}=={k:costs[k]-b for k,b in budget.items()}
 for cat in ("salary","seats","travel","vendor_credit"):
  assert cents(c["category_totals"][cat]["amount"])==sum(v[cat] for v in want.values())
 dims={}
 for ix,n in enumerate(("client","team","contract")):
  y=defaultdict(int)
  for key,v in costs.items():y[meta[key][ix]]+=v
  assert sum(y.values())==total
  dims[n]={k:cash(v) for k,v in sorted(y.items())}
 info=dict(period=s["period"],rows=len(rows),contract_targets=len(want),pools=len(pools),allocations=len(seen),
    total=cash(total),budget=cash(sum(budget.values())),variance=cash(total-sum(budget.values())),
    vendor_credit=cash(sum(v["vendor_credit"] for v in want.values())),dimensions=dims)
 return info,{k:cash(v) for k,v in costs.items()}

def adverse():
 rows,s,*_=build(PERIODS[0])
 a=run(rows,s)
 def obj(r,k):return next(x for x in r["objects"] if x["object_id"]==k)
 # Credit is a supplier offset to expense, not a customer revenue receipt.
 assert D(obj(a,"C")["total_cost"])==590 and D(obj(a,"C")["revenue"])==1200
 cov=a["coverage"];cells={(x["scope"],x["category"]):x["state"] for x in cov["cells"]}
 assert cells[("D","vendor_credit")]=="verified_zero" and cells[("D","salary")]=="not_applicable"
 bad=copy.deepcopy(s);del bad["completeness"]["observations"]["D"]["vendor_credit"]
 incomplete=run(rows,bad);assert incomplete["ledger_reconciled"] and incomplete["coverage"]["status"]=="incomplete"
 assert any(x["code"]=="coverage_required_missing" for x in incomplete["coverage"]["issues"])
 bad=copy.deepcopy(s);bad["completeness"]["period"]="2026-10"
 assert any(x["code"]=="coverage_control_period_mismatch" for x in run(rows,bad)["coverage"]["issues"])
 for kind in ("cross_month","zero_weight","negative_weight","duplicate","unknown_target"):
  b=copy.deepcopy(rows);t=copy.deepcopy(s)
  if kind=="cross_month":b[0]["month"]="2026-10"
  elif kind=="duplicate":b.append(dict(b[0]))
  elif kind=="zero_weight":t["allocation_rules"][0]["weights"]={"C":0,"D":0}
  elif kind=="negative_weight":t["allocation_rules"][0]["weights"]={"C":-2,"D":1}
  else:t["allocation_rules"][0]["target_ids"][0]="unowned"
  try:run(b,t);raise AssertionError(kind+" unexpectedly succeeded")
  except ValueError:pass
 b=copy.deepcopy(rows)
 for row in b:
  if row["object_id"]=="C":row["price"]="-150"
 try:
  n=obj(run(b,s),"C")
  assert D(n["revenue"])==-1200 and D(n["total_cost"])==590
  neg="explicit signed selling_price produces negative modeled revenue; not a credit-note ledger"
 except ValueError:neg="negative selling_price rejected by current contract"
 return dict(vendor_credit_preserves_revenue=True,evidenced_zero=True,NA=True,missing_but_balanced=True,mixed_month_rejected=True,
             month_of_controls_checked=True,weights_invalid_rejected=True,duplicate_rejected=True,foreign_target_rejected=True,negative_revenue=neg)

if __name__=="__main__":
 root=Path("fixtures/professional"); result={"seed":SEED,"industry_code":"7231","months":[],"adverse":adverse()}
 for month,golden in zip(PERIODS,[{"C":"590","D":"190"},{"C":"180","D":"450"}]):
  for large in (False,True):
   data=build(month,large);info,out=confirm(data)
   if not large:assert out==golden,(out,golden)
   prefix=f"{month}-"+("large" if large else "web")
   (root/(prefix+".csv")).write_text(csvtext(data[0]),encoding="utf8")
   (root/(prefix+".schema.json")).write_text(json.dumps(data[1],ensure_ascii=False,separators=(",",":")),encoding="utf8")
   result["months"].append(dict(variant=prefix,**info))
   print(prefix,len(data[0]),info["contract_targets"],info["allocations"],info["total"],info["budget"],info["variance"],info["vendor_credit"],flush=True)
 (root/"acceptance.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
 print(result["adverse"])
