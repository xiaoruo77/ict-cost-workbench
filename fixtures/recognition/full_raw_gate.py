import json,resource,tempfile,time,sys
from pathlib import Path
from decimal import Decimal
import copy
sys.path.insert(0,str(Path(__file__).resolve().parent))
from probe_adapter_scaling import make_rows,csv_bytes,config
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot,load_snapshot,export_snapshot
def measure(f,*a):
    t=time.perf_counter();r=f(*a);return r,round(time.perf_counter()-t,3)
rows=make_rows(5844)
data=csv_bytes(rows); schema=config(); d="csv-12a12a12a12a1234"; schema["dataset_id"]=d
assert len(data.encode())<10*1024*1024 and len(json.dumps(schema).encode())<256*1024
req,p1=measure(CsvCostingAdapter().from_text,data,schema)
result,p2=measure(CostingEngine().analyze,req)
with tempfile.TemporaryDirectory() as a:
    root=Path(a)
    ds=root/"costing_datasets"/d;ds.mkdir(parents=True)
    (ds/"data.csv").write_text(data,encoding="utf8")
    (ds/"manifest.json").write_text(json.dumps({"schema":schema}),encoding="utf8")
    context=dict(dataset_id=d,period=schema["period"],scenario="actual",owner_external_id="fake-workspace",dataset_label="synthetic")
    snap,p3=measure(create_snapshot,result,root,root,context)
    same,p4=measure(load_snapshot,snap["analysis_id"],root)
    assert same["revision"]==snap["revision"]
    assert Decimal(same["result"]["totals"]["total_cost"])==11688
    raw=[e for e in same["evidence"] if e["source_uri"]=="upload://data.csv"]
    assert len(raw)==5844 and {e["rows"][0]["values"]["id"] for e in raw}=={x[0] for x in rows}
    j,p5=measure(export_snapshot,same,"json",root/"export")
    m,p6=measure(export_snapshot,same,"md",root/"export")
    assert json.loads(j.read_bytes())["revision"]==same["revision"]
    md=m.read_text()
    assert all(w in md for w in ("11,688.00","成本确认与期间结转","不代表未申报交易"))
    outside=root/"isolated-other-owner"
    try:load_snapshot(snap["analysis_id"],outside)
    except FileNotFoundError:pass
    else:raise AssertionError("read escaped own workspace")
    print(json.dumps(dict(raw_rows=len(rows),csv_bytes=len(data.encode()),schema_bytes=len(json.dumps(schema).encode()),
        parse_s=p1,engine_s=p2,snapshot_s=p3,load_s=p4,json_export_s=p5,md_export_s=p6,json_bytes=j.stat().st_size,
        md_bytes=m.stat().st_size,peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,raw_evidence=len(raw),
        same_revision=True,expected_total="11688.00"),ensure_ascii=False))
big=csv_bytes(make_rows(10001))
try:CsvCostingAdapter().from_text(big,config())
except ValueError as e:assert "10000" in str(e)
else:raise AssertionError("raw size cap absent")
bad=copy.deepcopy(schema);bad["raw_recognition"]["excluded_tags"]=False
try:CsvCostingAdapter().from_text(data,bad)
except ValueError as e:assert "exclusions" in str(e)
else:raise AssertionError("unsafe malformed contract accepted")
