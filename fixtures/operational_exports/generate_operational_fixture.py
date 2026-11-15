"""Independent synthetic operational-export fixture; no costing engine imports."""
import argparse,csv,hashlib,io,json
from pathlib import Path
SCOPE={"book":"SYNTH_LOGISTICS","period":"2026-11","currency":"CNY"}
def csv_text(rows):
    stream=io.StringIO(newline="")
    writer=csv.DictWriter(stream,fieldnames=list(rows[0]),lineterminator="\n")
    writer.writeheader();writer.writerows(rows)
    return stream.getvalue()
def fixture():
    def event(eid,oid,**fields):
        return dict(event_id=eid,service_id=oid,**SCOPE,action="measure",original_event_id="",status="data",**fields)
    tms=[event("TMS-001","route-self",leg_id="LEG-1",weight="14000",weight_unit="kg"),
         event("TMS-002","route-outsourced",leg_id="LEG-2",weight="8000",weight_unit="kg"),
         event("TMS-003","route-self",leg_id="LEG-1",weight="2000",weight_unit="kg")]
    tms[2].update(action="reverse",original_event_id="TMS-001")
    gps=[dict(event_id="GPS-001",**SCOPE,leg_id="LEG-1",odometer_start="1000",odometer_end="2000",distance_unit="km"),
         dict(event_id="GPS-002",**SCOPE,leg_id="LEG-2",odometer_start="5000",odometer_end="6000",distance_unit="km")]
    wms=[event("WMS-001","warehouse-A",pallets="10",pallet_unit="pallet",storage_start="2026-10-20T00:00:00Z",storage_end="2026-12-05T00:00:00Z")]
    orders=[event(f"ORD-{i:03}","delivery-city",order_id=f"ORDER-{i:03}",sku="SKU-ABSTRACT",quantity="1",unit="order") for i in range(1,101)]
    returns=[event(f"RET-{i:03}","reverse-return",original_order_id=f"ORDER-{i:03}",sku="SKU-ABSTRACT",quantity="1",unit="order") for i in range(1,21)]
    activity=[]
    for i,(oid,hours) in enumerate([("route-self",4),("route-outsourced",3),("warehouse-A",2),("delivery-city",2),("reverse-return",1)],1):
        activity.append(event(f"ACT-{i:03}",oid,work_start="2026-11-03T08:00:00Z",work_end=f"2026-11-03T{8+hours:02}:00:00Z"))
    files={name:csv_text(rows) for name,rows in dict(tms=tms,gps=gps,wms=wms,orders=orders,returns=returns,activity=activity).items()}
    fields={"id":"event_id","object_id":"service_id","book":"book","period":"period","currency":"currency","action":"action","original_id":"original_event_id","status":"status"}
    units={"tonne":({"mass":1},"1000"),"kg":({"mass":1},"1"),"km":({"length":1},"1000"),"m":({"length":1},"1"),
           "tonne_km":({"mass":1,"length":1},"1000000"),"second":({"time":1},"1"),"hour":({"time":1},"3600"),
           "day":({"time":1},"86400"),"pallet":({"pallet":1},"1"),"pallet_day":({"pallet":1,"time":1},"86400"),
           "order":({"count":1},"1"),"completed_order":({"count":1},"1"),"processed_return":({"count":1},"1")}
    spec={"version":1,"period_timezone":"UTC","rule_version":"operational-source-contract-1.0.0","policy_source":"approved synthetic dimension and event policy; not enterprise approval",
      "source_hashes":{n:hashlib.sha256(t.encode()).hexdigest() for n,t in files.items()},
      "unit_registry":{n:{"dimensions":d,"factor":f,**({"quantity_kind":n} if n in {"order","completed_order","processed_return"} else {})} for n,(d,f) in units.items()},
      "sources":{n:{"file":n,"fields":dict(fields)} for n in files if n!="gps"},
      "metrics":[
        {"id":"road-volume","source":"tms","unit":"tonne_km","joins":[{"source":"gps","left_field":"leg_id","right_field":"leg_id"}],
         "factors":[{"field":"weight","unit_field":"weight_unit","unit":"tonne"},
                    {"source":"gps","op":"difference","start_field":"odometer_start","end_field":"odometer_end","unit_field":"distance_unit","unit":"km"}]},
        {"id":"storage-volume","source":"wms","unit":"pallet_day","factors":[{"field":"pallets","unit_field":"pallet_unit","unit":"pallet"},
          {"op":"duration","start_field":"storage_start","end_field":"storage_end","unit":"day","clip_to_period":True}]},
        {"id":"completed-deliveries","source":"orders","unit":"completed_order","factors":[{"field":"quantity","unit_field":"unit","unit":"order"}]},
        {"id":"processed-returns","source":"returns","unit":"processed_return","factors":[{"field":"quantity","unit_field":"unit","unit":"order"}]},
        {"id":"shared-work-hours","source":"activity","unit":"hour","factors":[{"op":"duration","start_field":"work_start","end_field":"work_end","unit":"hour"}]}],
      "object_bindings":[
        {"object_id":"route-self","metric_id":"road-volume","quantity_unit":"tonne_km","kind":"service"},
        {"object_id":"route-outsourced","metric_id":"road-volume","quantity_unit":"tonne_km","kind":"service"},
        {"object_id":"warehouse-A","metric_id":"storage-volume","quantity_unit":"pallet_day","kind":"service"},
        {"object_id":"delivery-city","metric_id":"completed-deliveries","quantity_unit":"completed_order","kind":"service"},
        {"object_id":"reverse-return","metric_id":"processed-returns","quantity_unit":"processed_return","kind":"service"}],
      "driver_bindings":[{"rule_id":"regional-to-completed-services","metric_id":"shared-work-hours","quantity_unit":"hour",
                          "target_ids":["route-self","route-outsourced","warehouse-A","delivery-city","reverse-return"]}],
      "relations":[{"id":"return-to-delivery","source":"returns","original_source":"orders","reference_field":"original_order_id",
                    "original_id_field":"order_id","quantity_field":"quantity","unit_field":"unit","original_quantity_field":"quantity",
                    "original_unit_field":"unit","unit":"order","same_fields":{"sku":"sku"}}]}
    spec["sources"]["gps"]={"file":"gps","role":"lookup","fields":{"id":"event_id","book":"book","period":"period","currency":"currency"}}
    expected={"oracle":"independent event arithmetic: (14t-2t)*1000km=12000;8t*1000km=8000;10pallets*30Novdays=300;100deliveries;20returns;hours4:3:2:2:1",
      "quantities":{"route-self":"12000","route-outsourced":"8000","warehouse-A":"300","delivery-city":"100","reverse-return":"20"},
      "driver_weights":{"route-self":"4","route-outsourced":"3","warehouse-A":"2","delivery-city":"2","reverse-return":"1"},
      "source_rows":{"tms":3,"gps":2,"wms":1,"orders":100,"returns":20,"activity":5},"return_events":20}
    return files,spec,dict(SCOPE),expected
def main():
    p=argparse.ArgumentParser();p.add_argument("--out",type=Path,required=True);a=p.parse_args()
    files,spec,scope,expected=fixture();a.out.mkdir(parents=True,exist_ok=True)
    for name,text in files.items():(a.out/(name+".csv")).write_bytes(text.encode("utf-8"))
    for name,value in {"operational_inputs.json":spec,"expected-operational.json":expected,"scope.json":scope}.items():
        (a.out/name).write_bytes(json.dumps(value,ensure_ascii=False,indent=2).encode("utf-8"))
    print(json.dumps({"files":list(files),"rows":sum(expected["source_rows"].values()),"out":str(a.out)},ensure_ascii=False))
if __name__=="__main__":main()