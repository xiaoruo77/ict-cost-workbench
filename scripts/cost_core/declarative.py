"""Versioned data-only templates compiled to the existing costing engine."""
from decimal import Decimal, InvalidOperation
import re
from .costing import CostingInput, CostObject, CostEntry, CostPool, CostDriver, AllocationRule
from .industry_profiles import load_profile
from .industry_catalog import VERSION, industry

CONTRACT_VERSION = "1.0.0"
MODULES = dict(zip(
    "materials labor utilities logistics overhead depreciation rnd quality trial loss regulatory equipment selling finance outsourcing cloud".split(),
    "材料 人工 能源 物流 管理 折旧 研发 质检 试验 损耗 合规 设备 销售 财务 外包 云服务".split()))
FIXED = {"overhead","depreciation","rnd","regulatory","equipment","finance"}
FIELDS = ("object_id","object_name","quantity","selling_price","module","amount","quantity_unit","object_kind")

def contract(code):
    item = industry(code)
    profile = load_profile(code) if item["level"] == 3 else None
    if profile is not None:
        if set(FIELDS).intersection(f["id"] for f in profile["fields"]):
            raise ValueError("Reserved field in profile")
        return {"version":CONTRACT_VERSION,"taxonomy_version":VERSION,"industry":item,
                "coverage":"configured-template","fields":list(FIELDS),
                "custom_fields":profile["fields"],"allocation":profile["allocation"],
                "pools":profile.get("pools",{}),"metrics":profile.get("metrics",{}),
                "modules":[{"id":k,**v} for k,v in profile["modules"].items()]}
    defaults = "materials labor utilities logistics overhead depreciation loss".split()
    if code.startswith("27"):
        defaults += "rnd quality trial regulatory".split()
    return {"version": CONTRACT_VERSION, "taxonomy_version": VERSION,
            "industry": item, "fields": list(FIELDS), "recommended_modules": defaults,
            "coverage": "starter-template",
            "modules": [{"id": k, "label": v, "behavior": "fixed" if k in FIXED else "variable"}
                        for k,v in MODULES.items()]}

def _text(value, field):
    if not isinstance(value,str) or not value.strip() or len(value)>180:
        raise ValueError(field + " must be nonempty text")
    return value.strip()

def _num(value, field, optional=False):
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value,(str,int,Decimal)) or isinstance(value,bool) or len(str(value))>80:
        raise ValueError(field + " must be a decimal string or integer")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(field + " must be numeric") from exc
    if (not number.is_finite() or not -12 <= number.as_tuple().exponent <= 16
        or number and not -12 <= number.adjusted() <= 16):
        raise ValueError(field + " is outside supported precision or range")
    return number

def compile_input(payload):
    if not isinstance(payload,dict) or payload.get("version") != CONTRACT_VERSION:
        raise ValueError("Unsupported declarative contract version")
    if set(payload)-{"version","industry_code","period","currency","rows","shared_amount","shared_pools"}:
        raise ValueError("Unknown request fields")
    code = _text(payload.get("industry_code"),"industry_code")
    schema = contract(code)
    if schema["industry"]["level"] != 3:
        raise ValueError("Choose a four-digit industry subclass")
    period = _text(payload.get("period"),"period")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])",period):
        raise ValueError("period must be YYYY-MM")
    currency = payload.get("currency","CNY")
    if not isinstance(currency,str) or not re.fullmatch("[A-Z]{3}",currency):
        raise ValueError("currency must be a three-letter code")
    rows = payload.get("rows")
    if not isinstance(rows,list) or not 1<=len(rows)<=100000:
        raise ValueError("rows must contain 1 to 100000 items")
    objects = {}; entries = []
    fields = schema.get("custom_fields",[])
    mods = {m["id"]:m for m in schema["modules"]}
    alloc = schema.get("allocation")
    drivers = {}
    allowed = set(FIELDS) | {f["id"] for f in fields}
    for i,row in enumerate(rows,1):
        if not isinstance(row,dict) or set(row)-allowed:
            raise ValueError(f"row {i}: invalid fields")
        key = _text(row.get("object_id"),f"row {i} object_id")
        name = _text(row.get("object_name"),f"row {i} object_name")
        quantity = _num(row.get("quantity"),"quantity",True)
        price = _num(row.get("selling_price"),"selling_price",True)
        if (quantity is not None and quantity<0) or (price is not None and price<0):
            raise ValueError(f"row {i}: negative quantity or price")
        attrs = {}
        for f in fields:
            value = row.get(f["id"])
            if value in (None,"") and not f["required"]:
                continue
            attrs[f["id"]] = (_num(value,f["id"]) if f["type"]=="decimal" else _text(value,f["id"]))
        if key == "_shared":
            raise ValueError("Reserved object id")
        unit = _text(row.get("quantity_unit") or "unit","quantity_unit")
        kind = _text(row.get("object_kind") or "product","object_kind")
        if kind not in ("product","service","project","customer","batch"):
            raise ValueError("Unsupported cost-object kind")
        obj = CostObject(object_id=key,name=name,kind=kind,quantity=quantity,quantity_unit=unit,currency=currency,selling_price=price,attributes=attrs)
        if alloc:
            weight = attrs.get(alloc["driver"])
            if weight is None or weight < 0:
                raise ValueError("Missing or negative cost driver")
            drivers[key] = weight
        if key in objects and objects[key] != obj:
            raise ValueError(f"row {i}: conflicting object definition")
        objects[key] = obj
        module = _text(row.get("module"),"module")
        if module not in mods:
            raise ValueError(f"row {i}: unknown cost module")
        fixed = mods[module]["behavior"] == "fixed"
        amount = _num(row.get("amount"),"amount")
        entries.append(CostEntry(entry_id=str(i),object_id=key,element_id=module,amount=amount,
                                 currency=currency,behavior="fixed" if fixed else "variable",
                                 cost_type=mods[module].get("cost_type","indirect" if fixed else "direct")))
    pools, cost_drivers, rules = [], [], []
    configs = schema.get("pools", {})
    amounts = payload.get("shared_pools",{})
    if not isinstance(amounts,dict) or set(amounts)-set(configs):
        raise ValueError("Unknown shared pool")
    jobs = [(key,configs[key],amount) for key,amount in amounts.items()]
    if "shared_amount" in payload:
        if not alloc:
            raise ValueError("No allocation rule in the chosen profile")
        jobs.append(("_legacy",alloc,payload["shared_amount"]))
    target_ids = tuple(objects)
    if jobs:
        objects["_shared"] = CostObject(object_id="_shared",name="Shared costs",kind="cost_center",currency=currency)
    for key,config,value in jobs:
        amount = _num(value, "shared_amount")
        weights = {k: objects[k].attributes.get(config["driver"]) for k in target_ids}
        if amount < 0 or any(v is None or v < 0 for v in weights.values()) or not any(weights.values()):
            raise ValueError("Invalid shared amount or driver values")
        m = mods[config["module"]]
        pools.append(CostPool(pool_id=key,name=key,owner_id="_shared",amount=amount,currency=currency,
                              element_id=config["module"],behavior=m["behavior"],cost_type=m["cost_type"]))
        cost_drivers.append(CostDriver(driver_id=key,name=config["driver"],unit="unit",values=weights))
        rules.append(AllocationRule(rule_id=key,pool_id=key,target_ids=target_ids,driver_id=key))
    return CostingInput(dataset_id="declarative-"+code,plugin_id="declarative-v1",
                        period=period,scenario="actual",currency=currency,
                        objects=tuple(objects.values()),entries=tuple(entries),pools=tuple(pools),drivers=tuple(cost_drivers),rules=tuple(rules))


def configured_metrics(request, schema):
    metrics = {}
    for key,spec in schema.get("metrics",{}).items():
        values = [obj.attributes.get(spec["field"]) for obj in request.objects if obj.object_id != "_shared"]
        total = sum(v for v in values if v is not None)
        value = None if not values or None in values else str(total if spec["op"]=="sum" else total/len(values))
        metrics[key] = {"value":value,"status":"missing" if value is None else "observed",
                        "op":spec["op"],"field":spec["field"]}
    return metrics
