"""Immutable, per-workspace costing results, grounded answers and exports.

No industry reader or model is called here. Every view consumes the same saved
result and the same captured source rows. The caller supplies an authenticated
workspace; a public sample never gives access to another workspace.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode


SCHEMA_VERSION = "costing-snapshot/1"
_RECOGNITION_RENDER_VERSION = 2
_RECOGNITION_SCOPE = "以下桥接仅对应本次申报的账簿及期间范围，不因本页筛选某一成本对象而改变。"
_HISTORICAL_RECOGNITION_SCOPE = "以下桥接属于完整的声明账簿及当前期间，不因本页筛选某一成本对象而改变。"
_ID = re.compile(r"[0-9a-f]{32}")
_DATASET = re.compile(r"csv-[0-9a-f]{16}")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else _json(value).encode("utf-8")).hexdigest()


def _inside(base, candidate):
    base, candidate = Path(base).resolve(), Path(candidate)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base) or candidate.is_symlink():
        raise ValueError("来源或结果路径越出当前工作区")
    return resolved


def _store(workspace_root):
    root = Path(workspace_root).resolve()
    folder = _inside(root, root / "costing_snapshots")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _refs(value):
    if isinstance(value, dict):
        if {"dataset_id", "source_uri"}.issubset(value) and isinstance(value["source_uri"], str):
            yield {key: str(value.get(key, "")) for key in ("dataset_id", "source_uri", "field", "selector", "kind", "formula")}
        else:
            for child in value.values():
                yield from _refs(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _refs(child)


def _csv_rows(data, ref, parsed=None):
    """CSV record numbers are logical records (header=1), not physical lines."""
    parsed = parsed if parsed is not None else {}
    if "rows" not in parsed:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("无法解码来源 CSV")
        reader = csv.DictReader(io.StringIO(text))
        parsed.update(headers=reader.fieldnames or [], rows=dict(enumerate(reader, 2)), indexes={})
    headers, all_rows = parsed["headers"], parsed["rows"]
    selector = dict(parse_qsl(ref.get("selector", ""), keep_blank_values=True))
    row_number = selector.get("row") or selector.get("CSV记录")
    chosen = []
    field = ref.get("field", "")
    filters = {k: v for k, v in selector.items() if k in headers}
    if row_number and str(row_number).isdigit():
        candidates = {int(row_number)} if int(row_number) in all_rows else set()
    else:
        candidates = None
        for key, value in filters.items():
            if key not in parsed["indexes"]:
                index = {}
                for n, row in all_rows.items():
                    index.setdefault(str(row.get(key, "")).strip(), set()).add(n)
                parsed["indexes"][key] = index
            matching = parsed["indexes"][key].get(value, set())
            candidates = set(matching) if candidates is None else candidates & matching
        candidates = candidates or set()
    for index in sorted(candidates):
        row = all_rows[index]
        if any(str(row.get(k, "")).strip() != v for k, v in filters.items()):
            continue
        # A reference with no usable selector cannot silently select all rows.
        if not row_number and not filters:
            continue
        chosen.append({"record": index, "values": row,
                       "field": field, "value": row.get(field) if field in row else None})
    return headers, chosen


def _is_extended_configuration_selector(selector):
    return isinstance(selector, str) and any(
        selector == root or selector.startswith(root + ".") or selector.startswith(root + "[")
        for root in ("raw_composite.operational_inputs", "business_views.financial_ledgers"))


def _extended_configuration_value(mapping, selector):
    """Traverse only declared mapping/list keys, never Python expressions."""
    if not isinstance(selector, str) or len(selector) > 2048:
        raise KeyError("invalid configuration selector")
    if not re.fullmatch(
            r"(?:raw_composite\.operational_inputs|business_views\.financial_ledgers)"
            r"(?:\.[A-Za-z_][A-Za-z0-9_-]{0,63}|\[[0-9]{1,5}\])*", selector):
        raise KeyError(selector)
    tokens = re.findall(r"([A-Za-z_][A-Za-z0-9_-]*)|\[([0-9]+)\]", selector)
    if len(tokens) > 32:
        raise KeyError("configuration selector depth")
    selected = mapping
    for name, index in tokens:
        if name:
            if not isinstance(selected, dict) or name.startswith("__"):
                raise KeyError(name)
            selected = selected[name]
        else:
            if not isinstance(selected, list):
                raise KeyError(index)
            selected = selected[int(index)]
    return selected


def _capture_ref(ref, workspace_root, source_root, dataset_id, cache=None):
    evidence = dict(ref)
    evidence["evidence_id"] = "e-" + _sha(ref)[:20]
    if ref.get("dataset_id") != dataset_id:
        raise ValueError("分析包含其他数据集的来源")
    uri = ref["source_uri"].replace("\\", "/")
    cache = cache if cache is not None else {}
    scope_key = ("validated_roots", Path(workspace_root), Path(source_root), dataset_id)
    if scope_key not in cache:
        cache[scope_key] = (Path(workspace_root).resolve(), Path(source_root).resolve())
    ws, shared = cache[scope_key]
    def safe_source(root, candidate):
        key = ("validated_file", scope_key, root, candidate)
        if key not in cache:
            safe = _inside(root, candidate)
            cache[key] = (safe, safe.is_file())
        return cache[key]
    path = None
    available = False
    if uri == "upload://data.csv":
        if not _DATASET.fullmatch(dataset_id):
            raise ValueError("上传来源缺少合法的个人数据集身份")
        path, available = safe_source(ws, ws / "costing_datasets" / dataset_id / "data.csv")
    elif uri.startswith('upload://sources/'):
        name = uri.removeprefix('upload://sources/')
        if not _DATASET.fullmatch(dataset_id) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}\.csv', name):
            raise ValueError('上传来源包含非法文件身份')
        path, available = safe_source(ws, ws / 'costing_datasets' / dataset_id / 'sources' / name)
    elif uri.startswith("raw/") and not any(x in {"", ".", ".."} for x in uri.split("/")):
        path, available = safe_source(shared / "raw", shared / uri)
    elif uri.startswith("derived:"):
        evidence.update(status="derived", description="确定性派生记录；依据同一事实列出的原始来源及公式复算，不冒充原始CSV行。")
        return evidence
    elif uri.startswith("sample://"):
        evidence.update(status="synthetic", description="代码内的可重复模拟数据；不是企业凭证或实际经营记录。")
        return evidence
    elif uri.startswith(("schema://", "upload://schema", "mapping://")):
        if _DATASET.fullmatch(dataset_id):
            manifest, present = safe_source(ws, ws / "costing_datasets" / dataset_id / "manifest.json")
            if present:
                mk = ("manifest", scope_key, manifest)
                if mk not in cache:
                    raw_manifest = manifest.read_bytes()
                    cache[mk] = (json.loads(raw_manifest.decode("utf-8")).get("schema", {}), _sha(raw_manifest))
                mapping, manifest_sha = cache[mk]
                selector = ref.get("selector", "")
                rollup=re.fullmatch(r"raw_composite\.object_rollups\[(\d{1,2})\]",selector) if isinstance(selector,str) else None
                if _is_extended_configuration_selector(selector):
                    try:
                        selected = _extended_configuration_value(mapping, selector)
                    except (KeyError, TypeError, IndexError):
                        evidence.update(status="selector_not_found", sha256=manifest_sha)
                        return evidence
                    mapping = {"scope": selector, "configuration": selected}
                elif "raw_composite" in mapping and ref.get("field")=="object_rollups" and rollup:
                    try:
                        data=mapping["raw_composite"]["object_rollups"][int(rollup[1])]
                    except (TypeError,KeyError,IndexError):
                        evidence.update(status="selector_not_found",sha256=manifest_sha)
                        return evidence
                    mapping={"scope":selector,"configuration":data}
                elif "raw_composite" in mapping and ref.get("field")=="object_budgets" and isinstance(selector,str) and (
                        budget:=re.fullmatch(r"raw_composite\.object_budgets\.rows\[(\d{1,2})\]",selector)):
                    try:
                        data=mapping["raw_composite"]["object_budgets"]["rows"][int(budget[1])]
                    except (TypeError,KeyError,IndexError):
                        evidence.update(status="selector_not_found",sha256=manifest_sha)
                        return evidence
                    mapping={"scope":selector,"configuration":data}
                elif "raw_composite" in mapping and ref.get("field")=="category_checklist" and isinstance(selector,str) and (
                        item:=re.fullmatch(r"raw_composite\.category_checklist\.groups\.([A-Za-z0-9_-]{1,40})\[(\d{1,2})\]",selector)):
                    try:
                        data=mapping["raw_composite"]["category_checklist"]["groups"][item[1]][int(item[2])]
                    except (KeyError,IndexError,TypeError):
                        evidence.update(status="selector_not_found",sha256=manifest_sha)
                        return evidence
                    mapping={"scope":selector,"configuration":data}
                elif selector == 'business_views' and 'business_views' in mapping:
                    mapping = {'scope': selector, 'configuration': mapping['business_views']}
                elif 'raw_composite' in mapping and isinstance(selector,str) and (global_item := re.fullmatch(r'raw_composite\.(allocation_rules|objects|incoming_pools)\[(\d{1,4})\]',selector)):
                    try:
                        data = mapping['raw_composite'][global_item[1]][int(global_item[2])]
                    except (KeyError,IndexError,TypeError):
                        evidence.update(status='selector_not_found',sha256=manifest_sha)
                        return evidence
                    mapping = {'scope': selector, 'configuration': data}
                elif selector == 'raw_composite.allocation_rules' and 'raw_composite' in mapping:
                    mapping = {'scope': selector, 'configuration': mapping['raw_composite'].get('allocation_rules', [])}
                elif "raw_composite" in mapping and isinstance(selector,str) and selector.startswith("raw_composite."):
                    match=re.fullmatch(r"raw_composite\.inputs\.([a-zA-Z0-9_-]{1,40})\.(.+)",selector)
                    try:
                        if not match: raise KeyError(selector)
                        group=mapping["raw_composite"]["inputs"][match[1]]
                        sub=match[2]
                        stream=re.fullmatch(r"raw_inventory\.streams\[(\d{1,2})\]\.(opening|production_posting_map)",sub)
                        obj=re.fullmatch(r"raw_recognition\.target_objects\[(\d{1,4})\]\.quantity",sub)
                        rule=re.fullmatch(r"allocation_rules\[(\d{1,4})\](\.weight_factors)?",sub)
                        if sub=="raw_meter.profile":
                            data=group["raw_meter"]["profile"]
                        elif sub in ("raw_inventory.chronology","raw_inventory.transfer_policy","raw_inventory.transfer_history",
                                    "raw_inventory.production_policy","raw_inventory.production_posting_map"):
                            data=group["raw_inventory"][sub.split(".")[-1]]
                        elif sub=="raw_inventory.opening":
                            data=group["raw_inventory"]["opening"]
                        elif stream:
                            data=group["raw_inventory"]["streams"][int(stream[1])][stream[2]]
                        elif obj:
                            data=group["raw_recognition"]["target_objects"][int(obj[1])]
                        elif rule:
                            data=group["allocation_rules"][int(rule[1])]
                            if rule[2]: data=data["weight_factors"]
                        else:
                            raise KeyError(selector)
                    except (TypeError,KeyError,IndexError):
                        evidence.update(status="selector_not_found",sha256=manifest_sha)
                        return evidence
                    mapping={"scope":selector,"configuration":data}
                elif "raw_recognition" in mapping and isinstance(selector,str):
                    obj=re.fullmatch(r"raw_recognition\.target_objects\[(\d{1,4})\]\.quantity",selector)
                    if obj:
                        try:
                            mapping={"scope":selector,"configuration":mapping["raw_recognition"]["target_objects"][int(obj[1])]}
                        except (TypeError,KeyError,IndexError):
                            evidence.update(status="selector_not_found",sha256=manifest_sha)
                            return evidence
                elif "raw_meter" in mapping and ref.get("field")=="raw_meter" and selector=="raw_meter.profile":
                    mapping={"scope":selector,"configuration":mapping["raw_meter"]["profile"]}
                elif "raw_inventory" in mapping and ref.get("field")=="raw_inventory" and isinstance(selector,str):
                    scope=selector
                    match=re.fullmatch(r"raw_inventory\.streams\[(\d{1,2})\]\.(opening|production_posting_map)",scope)
                    try:
                        if scope in ("raw_inventory.chronology","raw_inventory.transfer_policy","raw_inventory.transfer_history",
                                     "raw_inventory.production_policy","raw_inventory.production_posting_map"):
                            value=mapping["raw_inventory"][scope.split(".")[-1]]
                        elif scope=="raw_inventory.opening":
                            value=mapping["raw_inventory"]["opening"]
                        elif match:
                            value=mapping["raw_inventory"]["streams"][int(match[1])][match[2]]
                        else:
                            raise KeyError(scope)
                    except (TypeError,KeyError,IndexError):
                        evidence.update(status="selector_not_found",sha256=manifest_sha)
                        return evidence
                    mapping={"scope":scope,"configuration":value}
                elif "completeness" in mapping and isinstance(selector, str):
                    selected = mapping
                    path = []
                    if selector.startswith("completeness."):
                        path = selector.split(".")
                    elif ref.get("field") == "pool_options":
                        pairs = dict(parse_qsl(selector, keep_blank_values=True))
                        if set(pairs) == {"pool_id"}:
                            path = ["pool_options", pairs["pool_id"]]
                    else:
                        match = re.fullmatch(r"([a-z_]+)\[(\d+)\](?:\.([a-z_]+))?", selector)
                        if match:
                            path = [match[1], int(match[2])] + ([match[3]] if match[3] else [])
                    if path:
                        try:
                            for key in path:
                                selected = selected[key]
                        except (TypeError, KeyError, IndexError):
                            evidence.update(status="selector_not_found",sha256=manifest_sha)
                            return evidence
                        mapping = {"scope":selector,"configuration":selected}
                evidence.update(status="captured_mapping", mapping=mapping, sha256=manifest_sha)
                return evidence
    if path is None:
        evidence.update(status="reference_only", description="这是显式配置的外部来源标识，系统不主动访问该地址；需另附原始材料。")
        return evidence
    if not available:
        evidence.update(status="missing", description="当前授权来源目录中不存在该文件。")
        return evidence
    if path not in cache:
        data = path.read_bytes()
        cache[path] = {"data": data, "sha256": _sha(data), "parsed": {}}
    cached = cache[path]
    data = cached["data"]
    evidence.update(sha256=cached["sha256"], file_size=len(data))
    if path.suffix.lower() == ".csv":
        if ref.get("kind") == "synthetic" and "synthetic_join_label" in ref.get("selector", ""):
            evidence.update(status="derived", description="期间聚合产生的连接标签，非原始月份记录；真实输入行由同一事实的其他证据列出。")
            return evidence
        headers, rows = _csv_rows(data, ref, cached["parsed"])
        selector_keys = set(dict(parse_qsl(ref.get("selector", ""))))
        unknown_keys = selector_keys - set(headers) - {"row", "CSV记录", "object", "synthetic_join_label"}
        status = "captured" if rows else "selector_not_found"
        if ref.get("field") and ref["field"] not in headers:
            status = "field_not_found"
        if unknown_keys:
            status = "selector_field_not_found"
            evidence["unverified_selector_fields"] = sorted(unknown_keys)
        evidence.update(status=status, columns=headers, rows=rows,
                        description="CSV记录序号从表头1开始；多行引号字段仍按一条记录计数。")
    elif path.suffix.lower() == ".pdf":
        selector = dict(parse_qsl(ref.get("selector", "")))
        page_text = selector.get("page") or selector.get("页码")
        if page_text and page_text.isdigit():
            import fitz
            with fitz.open(stream=data, filetype="pdf") as doc:
                page = int(page_text)
                if not 1 <= page <= len(doc):
                    raise ValueError("PDF 来源页码越界")
                evidence.update(status="captured", page=page, text=doc[page - 1].get_text())
        else:
            evidence.update(status="page_not_specified", description="来源提供了PDF文件，但没有可核对的页码。")
    else:
        evidence.update(status="file_identified", description="已记录原始文件哈希；该来源未提供CSV行或PDF页选择器。")
    return evidence


def _number(value, places=2):
    if value is None:
        return "未知"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "未知"
        return format(number, f",.{places}f")
    except InvalidOperation:
        return str(value)


def _cell(value):
    # Keep user names as text in Markdown and in both exporters.
    value = str(value if value is not None else "未知").replace("\r", " ").replace("\n", " ")
    return (value.replace("|", "／").replace("<", "＜").replace(">", "＞").replace("`", "｀")
            .replace("\\", "＼").replace("[", "［").replace("]", "］"))


def _table(headers, rows):
    return ["| " + " | ".join(map(_cell, headers)) + " |",
            "|" + "|".join("---" for _ in headers) + "|"] + [
                "| " + " | ".join(map(_cell, row)) + " |" for row in rows]


def _source_ids(snapshot, value):
    # create_snapshot has already enumerated every result reference. Compute
    # stable IDs directly instead of rescanning all evidence per cost object.
    return sorted({"e-" + _sha(ref)[:20] for ref in _refs(value)})


def _extended_business_lines(result):
    """Render versioned auxiliary ledgers without reposting their balances."""
    view = result.get("business_views") or {}
    if view.get("presentation_version", 0) < 3:
        return []
    lines = ["", "### 运营计量与独立辅助账", "",
             "以下计量按上传原始行与声明契约复算；不代表真实系统或审批真实性已被独立核实。"
             "辅助账的现金、资产期末存量不再次加到成本归集额。", ""]
    operations = (result.get("recognition") or {}).get("operational_inputs")
    if operations and operations.get("status") == "requires_book_scope":
        lines += ["整套运营计量在全部对象范围展示。当前对象的原始依据保留在附录。", ""]
    elif operations:
        rows = operations.get("object_bindings", [])
        lines += _table(["对象", "计量规则", "数量", "单位", "资料状态"],
                        [[r.get("object_id"), r.get("metric_id"), _number(r.get("quantity"), 6),
                          r.get("unit") or r.get("quantity_unit"), r.get("status")] for r in rows]) + [""]
        if operations.get("boundary"):
            lines += [_cell(operations["boundary"]), ""]
    lines += ["入账路径与成本性质分列；直接入账加分摊转入等于归集路径，直接/间接性质来自成本类别，不能互相代替。", ""]
    lines += _table(["对象", "直接入账", "分摊转入", "分摊转出", "性质直接", "性质间接"],
                    [[r.get("object_id"), _number(r.get("direct_cost")), _number(r.get("allocated_in")),
                      _number(r.get("allocated_out")), _number((r.get("cost_type_structure") or {}).get("direct")),
                      _number((r.get("cost_type_structure") or {}).get("indirect"))] for r in result.get("objects", [])]) + [""]
    financial = view.get("financial_ledgers")
    if not financial:
        return lines
    if financial.get("status") == "requires_book_scope":
        return lines + ["辅助账需在全部对象范围查看；本对象报告不展示全账余额。", ""]
    lines += ["辅助账口径：" + _cell(financial.get("policy_source")), ""]
    settlement = financial.get("settlement")
    if settlement:
        labels = {"receivable": "应收", "payable": "应付", "agency": "代收代付"}
        rows = settlement.get("rows", [])
        lines += ["### 应收应付与代收代付", "",
                  "净开票、确认收入、现金收付分别保留。代收款不计为收入或成本；未批准的收入确认保持未知。", ""]
        lines += _table(["账户", "类型", "期初", "本期记账", "贷项", "期末"],
                        [[r.get("account_id"), labels.get(r.get("kind"), r.get("kind")),
                          *[_number(r.get(k)) for k in ("opening", "charges", "credits", "closing")]] for r in rows]) + [""]
        lines += _table(["账户", "收款", "付款", "净开票", "声明确认收入", "状态"],
                        [[r.get("account_id"), *[_number(r.get(k)) for k in ("cash_in", "cash_out", "net_billed", "net_revenue")], r.get("status")] for r in rows]) + [""]
        totals = settlement.get("totals") or {}
        lines += _table(["辅助账指标", "金额"],
                        [[label, _number(totals.get(key))] for label, key in (
                         ("应收余额", "receivable_closing"), ("应付余额", "payable_closing"),
                         ("代收待付余额", "agency_liability_closing"),
                         ("已声明账户现金净额", "net_cash_from_declared_accounts"),
                         ("净开票", "net_billed_receivables"), ("声明确认收入", "declared_net_revenue"))]) + [""]
        for row in settlement.get("cost_reconciliation", []):
            lines += ["成本勾稽：" + _cell(row.get("cost_element")) + "；账本 " + _number(row.get("expected_recognition"))
                      + "；成本引擎 " + _number(row.get("engine_cost")) + "；" + _cell(row.get("status")), ""]
    assets = financial.get("assets")
    if assets:
        rows = assets.get("rows", [])
        lines += ["### 自有资产与跨期履约成本", "",
                  "仅为声明的资产辅助账，不是完整资产负债表。购置、预付和资本化新增不再次入成本；"
                  "当期耗用、折旧、摊销、原履约资产转出与成本项逐一勾稽。", ""]
        lines += _table(["资产", "期初账面", "本期新增", "本期确认成本", "期末账面", "状态"],
                        [[r.get("asset_id"), *[_number(r.get(k)) for k in ("opening_value", "additions", "recognized_cost", "closing_value")], r.get("status")] for r in rows]) + [""]
        lines += _table(["资产", "类型", "期初数量", "期末数量", "单位"],
                        [[r.get("asset_id"), r.get("kind"), _number(r.get("opening_quantity"), 6),
                          _number(r.get("closing_quantity"), 6), r.get("unit")] for r in rows]) + [""]
        for row in assets.get("cost_reconciliation", []):
            lines += ["资产成本勾稽：" + _cell(row.get("cost_element")) + "；辅助账 " + _number(row.get("expected_recognition"))
                      + "；成本引擎 " + _number(row.get("engine_cost")) + "；" + _cell(row.get("status")), ""]
    bridge = financial.get("cost_bridge")
    if bridge:
        lines += ["### 成本与资产活动勾稽", ""] + _table(["指标", "金额"],
            [[label, _number(bridge.get(key))] for label, key in (
             ("成本引擎归集额", "engine_cost_total"), ("资产本期确认已含在成本", "asset_recognition_in_engine"),
             ("其他成本", "engine_other_costs"), ("独立资产期末余额", "separate_asset_closing"),
             ("批准本期履约资本化", "approved_service_capitalization"), ("原履约资产本期转成本", "prior_service_asset_release"))]) + [""]
    custody = financial.get("custody")
    if custody:
        lines += ["### 客户保管货物数量备查", "", "下列货物不是本服务商自有存货，不计为其货值成本或资产。", ""] + _table(
            ["货物", "期初数量", "期末数量", "单位", "状态"],
            [[r.get("goods_id"), _number(r.get("opening_quantity"), 6), _number(r.get("closing_quantity"), 6),
              r.get("unit"), r.get("status")] for r in custody.get("rows", [])]) + [""]
    return lines


def _report(snapshot, *, historical_recognition_scope=False):
    r, c = snapshot["result"], snapshot["context"]
    totals, currency = r["totals"], r.get("currency", "")
    name = _cell(c.get("dataset_label") or c["dataset_id"])
    lines = [f"# {name} 成本分析报告", "", "## 一、核算口径与版本", "",
             f"数据集：{_cell(c['dataset_id'])}；期间：{_cell(c['period'])}；情景：{_cell(c['scenario'])}；币种：{_cell(currency)}。",
             f"范围：{_cell(c.get('cost_object') or '全部成本对象')}。核算方法：{_cell(r.get('method', ''))}。",
             f"分析编号：{snapshot['analysis_id']}；数据与结果版本：{snapshot['revision']}。",
             "以下表格、图表、问答和来源附录均来自此不可变分析快照。金额展示保留两位小数，核算保留Decimal精度；精确值保存在快照JSON中。", "",
             "## 二、汇总与资料完整性", ""]
    if r.get('business_views'):
        lines[lines.index('## 二、汇总与资料完整性'):lines.index('## 二、汇总与资料完整性')] = [
            '行业配置：' + _cell(c.get('industry', '未声明')) + '；插件版本：' + _cell(c.get('plugin_version', '未声明')) + '。',
            '配置校验值：' + _cell(c.get('configuration_sha256', '未提供')) + '。', '']
    rows = [("成本发生额（含未结转资产）" if r.get('business_views') else "总成本", totals.get("total_cost")), ("总收入", totals.get("total_revenue")),
            ("已知收入", totals.get("known_revenue")), ("经营毛差", totals.get("total_operating_margin")),
            ("变动成本", totals.get("total_variable_cost")), ("固定成本", totals.get("total_fixed_cost")),
            ("未分配成本", totals.get("unallocated_cost"))]
    lines += _table(["指标", f"金额（{currency}）"], [(label, _number(value)) for label, value in rows])
    if totals.get("total_revenue") is None:
        lines += ["", "收入资料不足，无法判断整体盈利或亏损；已知部分收入不能代替总收入。"]
    if totals.get("total_variable_cost") is None or totals.get("total_fixed_cost") is None:
        lines += ["", "成本性态资料不完整，固定/变动成本、边际贡献和盈亏平衡结果以各字段的已知状态为准；未默认将混合成本按50%拆分。"]
    lines += ["", "## 三、成本对象与成本结构", ""]
    lines += _table(["对象", "数量/单位", "单位成本", "总成本", "收入", "经营毛差"], [
        [x["name"], f"{_number(x.get('quantity'), 4)} {x.get('quantity_unit', '')}", _number(x.get("unit_cost"), 4),
         _number(x.get("total_cost")), _number(x.get("revenue")), _number(x.get("operating_margin"))]
        for x in r.get("objects", [])])
    lines += [""] + _table(["成本类别", f"金额（{currency}）"], [(k, _number(v)) for k, v in totals.get("cost_structure", {}).items()])
    lines += ["", "## 四、分摊、差异与情景", "",
              "分摊记录保存每阶段金额、动因、原始成本类别和上游路径；成本中心转出后不重复计入最终成本。", ""]
    if r.get("allocations"):
        lines += _table(["阶段", "成本池→对象", "金额", "动因/公式"], [
            [x.get("stage"), f"{x.get('pool_name', x.get('pool_id'))} → {x.get('to_object')}", _number(x.get("amount")),
             x.get("formula", "")] for x in r["allocations"]])
    else:
        lines += ["本次没有分摊记录。"]
    if r.get("benchmarks"):
        lines += ["", "### 对标差异", ""] + _table(["对象", "基准情景", "本次成本", "基准成本", "差异"], [
            [x["object_id"], x.get("scenario"), _number(x.get("actual_cost")), _number(x.get("benchmark_cost")), _number(x.get("variance"))]
            for x in r["benchmarks"]])
        from decimal import Decimal
        budgets = [x for x in r["benchmarks"] if x.get("scenario") == "budget"]
        if budgets:
            actual = sum(Decimal(x["actual_cost"]) for x in budgets)
            budget = sum(Decimal(x["benchmark_cost"]) for x in budgets)
            delta = actual - budget
            lines += ["", "#### 预算对比小计（仅含有预算的对象）", "",
                      "差异 = 本次成本减去预算；正值表示超出预算。", ""]
            lines += _table(["匹配对象数", "本次成本", "预算成本", "差异"], [
                [len(budgets), _number(actual), _number(budget), _number(delta)]])
    coverage = r.get("coverage")
    if coverage:
        table_gap = [""] if c.get("report_layout_version") == 2 else []
        states={"observed":"已入账","verified_zero":"经证实为零","not_applicable":"不适用","missing":"缺失","conflict":"冲突"}
        lines += ["", "## 成本完整性核对", "",
            "仅针对插件声明的本月全量来源与责任范围。来源审核与独立控制额由导入方提供，不代表外部审计或行业所有潜在费用。范围未覆盖或存在缺项时，已有金额只是可核对部分。",
            "完整性状态："+("所声明范围已核对" if coverage["status"]=="complete_for_declared_scope" else "不完整，禁止按完整台账使用"), ""]
        lines += _table(["源台账","独立控制额","已映射","未映射","未分摊"],
            [[_number(coverage[k]) for k in ("ledger_total","controls_total","mapped_total","unmapped_total","unallocated_total")]])
        lines += [""] + _table(["责任范围","费用项","核对状态","入账金额","依据"], [
            [x["scope"],x["label"],states[x["state"]],_number(x["amount"]),x["reason"]]
            for x in coverage["cells"]])
        if coverage["issues"]:
            lines += ["", "异常代码："+", ".join(sorted({x["code"] for x in coverage["issues"]}))]
        if coverage["category_totals"]:
            lines += ["", "只在同一层汇总；父级与子项不能相加。"] + table_gap + _table(["费用","层级","金额"], [
                [x["label"],x["kind"],_number(x["amount"])] for _,x in sorted(coverage["category_totals"].items())])
        if coverage["analytical"]:
            lines += ["", "仅用于分析，不是追加成本。"] + table_gap + _table(["范围","非入账分析","金额","依据"], [
                [x["scope"],x["label"],_number(x["amount"]),x["evidence"]] for x in coverage["analytical"]])
        if coverage["comparisons"]:
            lines += ["", "预算差异 = 已入账金额减预算；数量/费率是给定数据的差异拆分，不是因果断言。"] + table_gap + _table(
                ["费用","实际","预算","差异","数量贡献","费率贡献","依据"],[
                [x["label"],*[_number(x.get(k)) for k in ("actual","budget","variance","usage_effect","rate_effect")],x["evidence"]]
                for x in coverage["comparisons"]])
    recognition = r.get("recognition")
    if recognition and c.get("recognition_render_version") in (1, _RECOGNITION_RENDER_VERSION):
        trace_inputs = (recognition["inputs"] if recognition.get("operation") == "raw_composite"
                        else {"原始认列": recognition})
        foreign = [(branch, row) for branch, block in sorted(trace_inputs.items())
                   for row in block.get("foreign_booked_trace", {}).get("rows", ())]
        if foreign:
            lines += ["### 原币凭证与上游已入账金额", "",
                      "仅显示申报方提供的原币凭证及已入账人民币金额；不计算汇率、不作税务判断、不重复入账，未独立验证。"
                      "各外币金额不得相加。", ""] + _table(
                ["来源","原币凭证","原始日期","原币","原币金额","原账面CNY","入账凭据","认列状态"],
                [[branch,row["origin_source_id"],row["origin_date"],row["origin_currency"],row["origin_amount"],
                  row["booked_amount"],row["booking_evidence_id"],
                  "已认列" if row["recognition_status"]=="recognized" else "未认列"]
                 for branch,row in foreign[:50]]) + [""]
            if len(foreign) > 50:
                lines += [f"表中仅展示前50笔（共{len(foreign)}笔）；请查同版本JSON及凭证。", ""]
        def transfer_lines(inventory):
            transfers=inventory.get("transfers")
            if not transfers:
                return []
            return [
                "仅支持同账期、截至时点均已关闭的成对调拨；两端凭证与期初状态由申报方提供。"
                "发出后至收到前为在途资产，不属于经营费用；到货按发出仓原账面计价，不重复取整。",
                "合并勾稽：期初资产＋外部入库＝出库净成本＋报损＋期末资产；"
                "以下配对调拨金额不计入外部入库、不再计费用。", "",
            ] + _table(["配对完成笔数","调拨账面金额"],
                       [[len(transfers["closed_pairs"]),_number(transfers["closed_value"])]]) + [""] + _table(
                ["调拨ID","发出仓","接收仓","物料","数量","单位","账面金额"],
                [[pair[key] for key in ("transfer_id","from_warehouse","to_warehouse","item","quantity","uom")]
                 +[_number(pair["amount"])] for pair in transfers["closed_pairs"]]) + [""]
        lines += ["", "## 成本确认与期间结转", "",
                  (_HISTORICAL_RECOGNITION_SCOPE if historical_recognition_scope else _RECOGNITION_SCOPE),
                  "经过验证的范围仅限所申报来源与规则，不代表未申报交易、财务报表、会计或税务合规。",
                  "已确认发生费用和派生的存货/损益口径有重叠，严禁把两组数字相加。", ""]
        inventory = recognition.get("inventory_valuation")
        if inventory:
            vals = inventory["outputs"]
            if inventory.get("chronology"):
                lines += [f"本次按各库存流内的 UTC 事件时间和流水序号排序，"
                          f"{inventory['chronology']['events_at_changed_stream_position']} 条记录的位置与上传顺序不同。"
                          "凭证仍指向原 CSV 记录；排序不修复原始凭证或账期错误。", ""]
            lines += ["存货为本次申报仓库/物料流水；未经独立库存清单核实。入库和期末余额不计经营费用，退货按原发出成本冲回。", ""]
            lines += _table(["存货勾稽","金额"], [[label,_number(vals[key])] for key,label in
                            (("available_value","期初加本期外部入库" if inventory.get("transfers")
                               else "期初加本期入库"),("net_cogs","净出库销售成本"),
                             ("shrink","报损成本"),("closing_value","期末存货，不入费用"),
                             ("reconciliation_difference","勾稽差额"))]) + [""]
            if inventory.get("streams"):
                lines += ["各仓库、物料和计量单位单独计价；只汇总同币金额，不相加库存数量。", ""] + _table(
                    ["仓库","物料","单位","期末数量","净出库成本","报损","期末资产"],
                    [[v["context"]["warehouse"],v["context"]["item"],v["context"]["uom"],
                      v["outputs"]["closing_quantity"],_number(v["outputs"]["net_cogs"]),
                      _number(v["outputs"]["shrink"]),_number(v["outputs"]["closing_value"])]
                     for v in inventory["streams"]]) + [""]
            lines += transfer_lines(inventory)
        meter=recognition.get("meter_billing")
        if meter:
            lines += ["计量分档只按申报用量与价格方案执行，未独立验证计量原始记录、账单或合同。表内组别原金额未单独取整，月净额最后统一舍入。", ""] + _table(
                ["计费组","用量","单位","分档数","未舍入用量费用"],
                [[key,d["quantity"],meter["unit"],len(d["bands"]),d["raw"]]
                 for key,d in meter["groups"].items()]) + [""]
        if recognition.get("operation")=="raw_composite":
            lines += ["## 组合台账", "",
                      "只合并已确认费用；分流派生余额不再次入账。各来源的实物数量和未舍入值不跨口径相加。凭证来源未经独立核验。", ""]
            lines += _table(["来源","已确认费用","说明"],[[name,_number(src["ledger_recognized_total"]),
                             "待核" if src["source_recognition_status"]=="partial" else "仅申报范围"]
                            for name,src in sorted(recognition["inputs"].items())]) + [""]
            if recognition.get("composite_version")==3:
                bills=[(name,src["meter_billing"]) for name,src in sorted(recognition["inputs"].items())
                       if src.get("meter_billing")]
                if bills:
                    lines += ["各计量账户独立应用费率和舍入；不同账户或单位的数量不合计。", ""] + _table(
                        ["计量来源","计费账户","单位","已确认金额"],
                        [[name,bill["scope"]["billing_account"],bill["unit"],_number(bill["reported_net"])]
                         for name,bill in bills]) + [""]
            rollups=recognition.get("object_rollups")
            if rollups:
                lines += ["### 跨来源成本对象归集", "",
                          "仅对分摊后的最终对象进行列报归集；不重记费用、不合并用量或计算跨口径单位成本。"
                          "未归组对象仍纳入本表勾稽，归组齐全也不等于原始申报完整。", ""] + _table(
                    ["归集对象","来源 / 成本对象","分摊后已确认成本"],
                    [[group["label"],m["input_name"]+" / "+m["object_id"],_number(m["total_cost"])]
                     for group in rollups["groups"] for m in group["members"]] +
                    [[group["label"]+" 合计","",_number(group["total_cost"])] for group in rollups["groups"]] +
                    [["已归组小计","",_number(rollups["grouped_total"])],
                     ["未归组小计","",_number(rollups["ungrouped_total"])],
                     ["台账合计","",_number(rollups["ledger_total"])]]) + [""]
                if rollups["ungrouped"]:
                    lines += _table(["未归组来源","成本对象","金额","原因"],
                        [[row["input_name"],row["object_id"],_number(row["total_cost"]),
                          "内部成本池所有者" if row["reason"]=="pool_owner" else "未申报归组"]
                         for row in rollups["ungrouped"]]) + [""]
            checklist=recognition.get("category_checklist")
            if checklist:
                lines += ["### 插件声明的成本类别覆盖", "",
                          "仅核对插件声明的类别与已入账成本，并不证明原始数据齐全。"
                          "缺资料不记为零；声明为零或不适用不代表独立核验。", ""]
                states={"observed":"已观察","missing":"缺资料","declared_zero":"声明为零",
                        "not_applicable":"不适用","conflict":"与账面冲突"}
                lines += _table(["归集对象","成本类别","覆盖状态","已有金额","组成笔数"],
                    [[group["group_id"],row["name"],states[row["state"]],row["amount"],row["component_count"]]
                     for group in checklist["groups"] for row in group["categories"]]) + [""]
                lines += _table(["归集对象","未分类金额","未分类组成笔数"],
                    [[row["group_id"],row["uncategorized_amount"],row["uncategorized_component_count"]]
                     for row in checklist["groups"]]) + [""]
                other=[(group["group_id"],row) for group in checklist["groups"]
                       for row in group.get("uncategorized_components",())]
                if other:
                    lines += _table(["归集对象","来源","账面成本项","未分类金额"],
                        [[gid,row["input_name"],row["element_id"],row["amount"]] for gid,row in other[:50]]) + [""]
                if len(other)>50:
                    lines += [f"未分类表只展示前50项（共{len(other)}项），全表见本版本JSON。",""]
                if checklist["unreviewed_group_ids"]:
                    lines += ["未声明检查的归集对象："+"、".join(_cell(x) for x in checklist["unreviewed_group_ids"])+"。",""]
            budgets=recognition.get("object_budgets")
            if budgets:
                summary=budgets["summary"]
                # Preserve exact post-allocation sub-cent variance; percentages were rounded by the engine.
                def money_exact(value):
                    if value is None:
                        return "未知 / 不适用"
                    raw=str(value)
                    left,point,right=raw.partition(".")
                    return ("-" if left.startswith("-") else "") + format(abs(int(left)),",") + (point+right if point else "")
                lines += ["### 申报预算与可比实际", "",
                          "预算仅为申报数据，未独立核验；只比较同一账簿、期间、币种及归组成员的最终成本。"
                          "未提供预算不等于零，预算为零时不算偏差率，不代表台账、预算或真实来源完整。", ""] + _table(
                    ["归集对象","实际成本","申报预算","金额偏差","偏差率 (%)","预算依据"],
                    [[row["group_label"],money_exact(row["actual_cost"]),money_exact(row["budget_cost"]),
                      money_exact(row["variance"]),money_exact(row["variance_pct"]),
                      (("申报为零 / " if row["budget_status"]=="evidenced_zero" else "") + row["budget_source_id"])
                      if row["budget_source_id"] is not None else "未提供"] for row in budgets["rows"]] +
                    [["配对小计",money_exact(summary["comparable_actual"]),money_exact(summary["comparable_budget"]),
                      money_exact(summary["comparable_variance"]),money_exact(summary["comparable_variance_pct"]),"仅配对对象"],
                     ["未配预算的归组实际",money_exact(summary["unmatched_group_actual"]),"—","—","—","未比较"],
                     ["未归组实际",money_exact(summary["ungrouped_ledger_actual"]),"—","—","—","未比较"],
                     ["台账实际合计",money_exact(summary["ledger_actual"]),"—","—","—","不代表全台账预算"]]) + [""]
            inventory_order=[
                [name,src["inventory_valuation"]["chronology"]["events_at_changed_stream_position"]]
                for name,src in sorted(recognition["inputs"].items())
                if src.get("inventory_valuation",{}).get("chronology")
            ]
            if inventory_order:
                lines += ["下列来源按各库存流内的 UTC 事件时间和流水序号排序。"
                          "凭证仍指向原 CSV 记录；排序不修复原始凭证或账期错误。", ""] + _table(
                             ["库存来源","流内位置与上传顺序不同的记录数"],inventory_order) + [""]
            memo=[]
            for name,src in sorted(recognition["inputs"].items()):
                if src.get("inventory_valuation"):
                    branch_inventory=src["inventory_valuation"]
                    memo.append([name,"期末存货资产",_number(branch_inventory["outputs"]["closing_value"])])
                    if 'net_production_consumption' in branch_inventory['outputs']:
                        memo.append([name,"净投料成本，已归集到生产对象",
                                     _number(branch_inventory['outputs']['net_production_consumption'])])
                    if branch_inventory.get("transfers"):
                        lines += ["库存来源："+name,""] + transfer_lines(branch_inventory)
                for key,view in sorted(src["derived_views"].items()):
                    memo.append([name,key,_number(view["amount"])])
            if memo:
                lines += _table(["来源","不重复入账的派生值","金额"],memo) + [""]
        if recognition["source_recognition_status"]=="partial":
            lines += ["已声明来源存在未认列或待核事项；当前结果只代表已入账部分，禁止把它当完整损益。", ""]
            if recognition["unrecognized_nonzero_sources"]:
                lines += _table(["待核来源","金额","事项"],[[(x["input_name"]+"：" if x.get("input_name") else "")+x["source_id"],_number(x["amount"]),x["reason"]]
                                for x in recognition["unrecognized_nonzero_sources"]]) + [""]
        if recognition.get("allocation_status")=="needs_review":
            lines += ["认列已完成的金额不等于已分摊到对象。存在待处理的成本池或动因，详见质量和审计。", ""]
        manifest_check = recognition.get("source_book_check")
        if manifest_check:
            msg={"matched":"经独立信任路径提供的账簿清单与货币凭证一致；不表示业务范围外成本或损益完整。",
                 "mismatch":"独立账簿清单与源凭证不符；不得据此宣称齐全。",
                 "unverified":"独立清单未获得可信验证。",
                 "not_provided":"未提供独立账簿清单，只能检查提交的来源。"}
            lines += [msg.get(manifest_check["status"],"账簿清单状态未知。"), ""]
        labels={"production_cost":"生产成本","completed_goods_cost":"完工成本",
                "cost_of_sales":"销售成本","pnl_cost":"当期损益成本",
                "production":"生产费用","period":"期间费用","cogs":"销售出库净成本","shrink":"存货报损","metered_usage":"分档用量费用","fixed_fee":"当月固定费用","vendor_credit":"供应商贷项"}
        lines += _table(["确认范围","金额","是否入发生额台账"],
                        [["确认费用台账",_number(recognition["ledger_recognized_total"]),"是"]]+
                        [[labels.get(k,k),_number(v),"已含在台账"] for k,v in sorted(recognition["posted_pool_totals"].items())]+
                        [[labels.get(k,k),_number(v["amount"]),"桥接展示，不再次入账"]
                         for k,v in sorted(recognition["derived_views"].items())])
        # Sorting preserves canonical report round-trip after sorted JSON writes.
        if recognition["memo_sources"]:
            lines += ["", "现金、资本交易或其他列作备查的来源不进入确认费用。", ""] + _table(
                ["原来源","金额","排除原因"],[[v["source_id"],_number(v["amount"]),v["reason"]] for v in recognition["memo_sources"]])
    if r.get('business_views'):
        view = r['business_views']
        extended = view.get('presentation_version', 0) >= 3
        lines += ['', '### 成本发生额、资产与本期损益', '',
                  ('下表只分区本期成本归集额。自有资产辅助账与现金结算另列，不与归集额重复相加。'
                   if extended else '成本发生额包含尚未结转的资产；下表为同一发生额的分区，不重复入账。仅适用批准的同质等价单位口径。'),
                  '口径来源：' + _cell(view['policy_source']), '']
        asset_label = ('归集额留存的生产资产' if extended else
                       '本期发生额留存的生产资产' if view.get('presentation_version') == 2 else '期末资产')
        lines += _table(['本期成本归集额' if extended else '成本发生额', asset_label, '本期损益成本费用', '期间费用', '服务成本'],
                        [[_number(view[k]) for k in ('incurred_total','asset_total','pnl_cost','period_expense','service_cost')]])
        if view.get('separate_inventory_assets'):
            lines += ['', '独立库存子账的期末余额来自其自身的采购和发出勾稽。'] + _table(
                ['库存来源', '期末库存余额'],
                [[item['input_name'], _number(item['closing_value'])] for item in view['separate_inventory_assets']])
        required = view.get('required_costs')
        if required:
            labels = {'observed': '已提供', 'explicit_zero': '凭证显式为零', 'missing': '缺失'}
            lines += ['', '### 对象必需成本', '']
            if required['status'] == 'incomplete':
                lines += ['资料不完整。发生额和资产桥接只涵盖已提供资料。', '']
            lines += _table(['对象', '成本项', '资料状态', '金额'],
                            [[row['object_id'], row['element_id'], labels[row['status']], _number(row['amount'])]
                             for row in required['rows']])
        if view.get('presentation_version', 0) < 4 or view['lifecycle']:
            lines += _table(['对象', '在制', '完工库存', '净销售成本', '报废损失', '合格单位成本'],
                            [[x['object_id'], *[_number(x[k]) for k in ('wip','finished_inventory','cost_of_sales','scrap_loss','qualified_unit_cost')]] for x in view['lifecycle']])
        if view.get('settlement'):
            lines += ['', '### 收入与结算对账', '', '佣金与支付费用已在成本凭证计入一次；平台补偿优惠按申报合同保留，不抵减商家收入。', '']
            lines += _table(['对象','营业额','商家优惠','平台承担优惠','退款','净收入','佣金','支付费','应结回款','实结回款','差额'],
                            [[x['object_id'], *[_number(x[k]) for k in ('gross','merchant_discount','platform_discount','refund','net_revenue','commission','payment_fee','expected_receipt','receipt','receipt_difference')]] for x in view['settlement']['rows']])
        for notice in view['notices']:
            lines += ['', _cell(notice['code']) + '：' + _cell(notice['message'])]
        if view.get('dispositions'):
            disposition = view['dispositions']
            lines += ['', '### 来源声明的互斥处置校验', '',
                      '仅验证批准来源中处置身份的一致性，不代表序列号逐台追溯。依据：' + _cell(disposition['identity_basis']), '']
            lines += _table(['对象','处置身份','动作','原始关联记录'],
                            [[x['object_id'],x['item_id'],x['action'],x['ref']] for x in disposition['records']])
    lines += _extended_business_lines(r)
    if r.get("standard_variances"):
        lines += ["", "### 标准成本差异", ""] + _table(["对象/类别", "实际用量", "标准用量", "价格差异", "用量差异", "总差异"], [
            [f"{x['object_id']}/{x['element_id']}", _number(x.get("actual_quantity"), 4), _number(x.get("standard_quantity"), 4),
             _number(x.get("price_variance")), _number(x.get("usage_variance")), _number(x.get("total_variance"))]
            for x in r["standard_variances"]])
    if r.get("forecast"):
        f = r["forecast"]
        lines += ["", "### 显式假设下的情景估算", "", "该结果是给定假设的算术推演，不代表实际未来业绩。",
                  _cell(_json(f.get("assumptions", {}))), ""] + _table(["情景成本", "情景收入", "情景毛差"], [
                      [_number(f.get("forecast_cost")), _number(f.get("forecast_revenue")), _number(f.get("forecast_margin"))]])
    lines += ["", "## 五、质量问题与结论边界", ""]
    if r.get("quality"):
        object_names = {obj["object_id"]: obj["name"] for obj in r.get("objects", [])}
        for item in r["quality"]:
            subject = object_names.get(item.get("object_id"), item.get("object_id"))
            prefix = f"{_cell(subject)}：" if subject else ""
            lines += [f"- {prefix}{_cell(item.get('code'))}：{_cell(item.get('message') or _json(item))}"]
    else:
        lines += ["当前已执行的核算检查未发现问题；这不证明原始业务凭证真实或未遗漏。"]
    unresolved = [e for e in snapshot["evidence"] if e["status"] not in {"captured", "captured_mapping", "synthetic", "derived"}]
    if unresolved:
        lines += [f"- 另有 {len(unresolved)} 个来源尚缺可直接核对的原始记录或页码，见附录状态；不得据此作确定归因。"]
    lines += ["", "没有直接证据时，本报告不把成本变动归因于设备、采购、效率或管理行为；这些解释需要同期间、同对象的补充凭证。", "",
              "## 六、证据索引与原始记录", "",
              "编号对应页面的来源核对入口；本附录同时包含文件SHA-256、CSV记录序号与列名或PDF页码，可离线复核。", ""]
    for obj in r.get("objects", []):
        ids = _source_ids(snapshot, obj)
        if ids:
            lines += [f"对象 {_cell(obj['name'])}：{', '.join(ids)}。", ""]
    for ev in snapshot["evidence"]:
        lines += [f"### {ev['evidence_id']}", "",
                  f"来源：{_cell(ev['source_uri'])}；字段：{_cell(ev.get('field'))}；选择器：{_cell(ev.get('selector'))}。",
                  f"状态：{_cell(ev['status'])}。{_cell(ev.get('description', ''))}"]
        if ev.get("sha256"):
            lines += [f"SHA-256：{ev['sha256']}"]
        if ev.get("formula"):
            lines += [f"推导：{_cell(ev['formula'])}"]
        if ev.get("rows"):
            lines += [""] + _table(["CSV记录", "列", "原始值"], [
                [row["record"], key, value] for row in ev["rows"] for key, value in row["values"].items() if key == ev.get("field") or not ev.get("field")])
        if ev.get("page"):
            lines += [f"PDF第 {ev['page']} 页：{_cell(ev.get('text', ''))}"]
        if ev.get("mapping"):
            lines += ["配置映射：" + _cell(_json(ev["mapping"]))]
        lines += [""]
    metrics = snapshot.get("result",{}).get("configured_metrics",{})
    if metrics:
        lines.extend(["", "## 行业配置指标", ""])
        for metric_id, item in sorted(metrics.items()):
            lines.append("- " + _cell(metric_id) + "：" + _cell(item["value"] if item["value"] is not None else "未知"))
    return "\n".join(lines)


def _verify_checklist_evidence(result, evidence):
    """Refuse a snapshot if its immutable checklist does not match its captured policy."""
    recognition=result.get("recognition") or {}
    checklist=recognition.get("category_checklist")
    if not checklist:
        return
    from cost_core.category_checklist import policy_sha256
    from cost_core.object_budgets import membership_sha256
    by_id={row["evidence_id"]:row for row in evidence}

    def config_for(refs, field):
        refs=[ref for ref in _refs(refs)
              if ref["field"]==field and ref["kind"]=="configuration"]
        if len(refs)!=1:
            raise ValueError("成本类别清单缺少唯一的配置凭证")
        row=by_id.get("e-"+_sha(refs[0])[:20],{})
        mapping=row.get("mapping") or {}
        config=mapping.get("configuration")
        if row.get("status")!="captured_mapping" or type(config) is not dict:
            raise ValueError("成本类别清单配置凭证无法核验")
        return config

    canonical={g["id"]:g for g in recognition["object_rollups"]["groups"]}
    for group in checklist["groups"]:
        gid=group["group_id"]
        source=config_for(canonical[gid]["source_refs"],"object_rollups")
        try:
            valid = (source.get("id")==gid and type(source.get("members")) is list and
                     membership_sha256(recognition["context"],gid,source["members"])==group["membership_sha256"])
        except (TypeError,KeyError,ValueError):
            valid=False
        if not valid:
            raise ValueError("成本类别清单归集配置与核算版本不一致")
        for category in group["categories"]:
            source=config_for(category["source_refs"],"category_checklist")
            if policy_sha256(source)!=category["policy_sha256"]:
                raise ValueError("成本类别清单配置与核算版本不一致")


def _verify_bundle_sources(result, workspace_root, dataset_id):
    bundle = (result.get('recognition') or {}).get('source_bundle')
    if bundle is None:
        return
    if not _DATASET.fullmatch(dataset_id):
        raise ValueError('多来源快照缺少个人数据集身份')
    root = _inside(workspace_root, Path(workspace_root) / 'costing_datasets' / dataset_id)
    manifest = json.loads(_inside(root, root / 'manifest.json').read_text(encoding='utf-8'))
    schema = manifest.get('schema', {})
    if _sha({k:v for k,v in schema.items() if k != 'dataset_id'}) != bundle.get('config_sha256'):
        raise ValueError('计算后规则配置发生变化，拒绝绑定旧金额')
    seen = set()
    for source in bundle.get('sources', []):
        name = source.get('name', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', name) or name in seen:
            raise ValueError('多来源快照文件身份重复或非法')
        seen.add(name)
        path = _inside(root, root / 'sources' / (name + '.csv'))
        if not path.is_file() or _sha(path.read_bytes()) != source.get('sha256'):
            raise ValueError('计算后原始来源发生变化：' + name)
    if not seen:
        raise ValueError('多来源快照缺少原始文件清单')


def create_snapshot(payload, workspace_root, source_root, context):
    result = copy.deepcopy(payload.get("result", payload))
    context = copy.deepcopy(context)
    for key in ("dataset_id", "period", "scenario"):
        if context.get(key) != result.get(key):
            raise ValueError(f"快照{key}与核算范围不一致")
    if not isinstance(result.get("totals"), dict) or not isinstance(result.get("objects"), list):
        raise ValueError("核算结果结构无效")
    if context.get("cost_object") and any(x.get("object_id") != context["cost_object"] for x in result["objects"]):
        raise ValueError("快照成本对象超出请求范围")
    refs = {_json(ref): ref for ref in _refs(result)}
    _verify_bundle_sources(result, workspace_root, context['dataset_id'])
    source_cache = {}
    evidence = [_capture_ref(ref, workspace_root, source_root, context["dataset_id"], source_cache) for _, ref in sorted(refs.items())]
    bundle = (result.get('recognition') or {}).get('source_bundle')
    if bundle:
        expected = {s['source_uri']: s['sha256'] for s in bundle['sources']}
        if any(ev.get('source_uri') in expected and ev.get('sha256') != expected[ev['source_uri']] for ev in evidence):
            raise ValueError('捕获的原始凭证与计算输入不一致')
    _verify_checklist_evidence(result,evidence)
    public_context = {key: value for key, value in context.items() if key not in {"revision"}}
    if result.get("coverage"):
        public_context["report_layout_version"] = 2
    if result.get("recognition"):
        public_context["recognition_render_version"] = _RECOGNITION_RENDER_VERSION
    revision = _sha({"result": result, "context": public_context, "evidence": evidence})
    public_context["revision"] = revision
    snapshot = {"schema_version": SCHEMA_VERSION, "analysis_id": uuid.uuid4().hex,
                "revision": revision, "context": public_context, "result": result, "evidence": evidence,
                "created_at": datetime.now(timezone.utc).isoformat()}
    snapshot["report"] = {"markdown": _report(snapshot), "analysis_id": snapshot["analysis_id"], "revision": revision}
    snapshot["charts"] = chart_data(snapshot)
    folder = _store(workspace_root)
    target = folder / (snapshot["analysis_id"] + ".json")
    # Unique name plus exclusive creation: existing records can never be replaced.
    temporary = target.with_suffix(".pending")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            output.write(_json(snapshot))
            output.flush()
            os.fsync(output.fileno())
        if target.exists():
            raise FileExistsError("分析编号已存在，拒绝覆盖")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return snapshot


def load_snapshot(analysis_id, workspace_root):
    if not isinstance(analysis_id, str) or not _ID.fullmatch(analysis_id):
        raise ValueError("分析编号无效")
    folder = _store(workspace_root)
    path = _inside(folder, folder / (analysis_id + ".json"))
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    return validate_snapshot_integrity(snapshot, analysis_id)


def validate_snapshot_integrity(snapshot, analysis_id):
    """Validate a captured snapshot in memory; no source refresh or filesystem writes."""
    if snapshot.get("schema_version") != SCHEMA_VERSION or snapshot.get("analysis_id") != analysis_id:
        raise ValueError("分析快照身份或格式错误")
    context = {key: value for key, value in snapshot["context"].items() if key != "revision"}
    expected = _sha({"result": snapshot["result"], "context": context, "evidence": snapshot["evidence"]})
    if expected != snapshot.get("revision") or expected != snapshot["context"].get("revision"):
        raise ValueError("分析快照校验失败")
    _verify_checklist_evidence(snapshot["result"],snapshot["evidence"])
    stored_markdown=snapshot.get("report",{}).get("markdown")
    matches=stored_markdown==_report(snapshot)
    if not matches:
        # A historical standalone recognition writer emitted exactly one
        # different paragraph under render v1. Re-render the entire report
        # from the sealed result, not by editing/trusting stored Markdown.
        rec=snapshot["result"].get("recognition") or {}
        eligible=(type(context.get("recognition_render_version")) is int and
                  context["recognition_render_version"]==1 and
                  type(rec.get("profile_version")) is int and rec["profile_version"]==1 and
                  rec.get("status")=="valid" and rec.get("ledger_basis")=="recognized_expenses_incurred" and
                  not any(k in rec for k in ("operation","inventory_valuation","meter_billing","foreign_booked_trace")))
        matches=eligible and stored_markdown==_report(snapshot,historical_recognition_scope=True)
    if not matches or snapshot.get("charts") != chart_data(snapshot):
        raise ValueError("分析快照的报告或图表与数据不一致")
    return snapshot


def resolve_evidence(snapshot, evidence_id, source_root, workspace_root):
    evidence = next((e for e in snapshot["evidence"] if e["evidence_id"] == evidence_id), None)
    if evidence is None:
        raise FileNotFoundError("该分析没有此来源编号")
    result = copy.deepcopy(evidence)
    result.update(analysis_id=snapshot["analysis_id"], revision=snapshot["revision"], context=snapshot["context"])
    # Return captured rows even when the original has changed; disclose that change.
    current = _capture_ref({k: evidence.get(k, "") for k in ("dataset_id", "source_uri", "field", "selector", "kind", "formula")},
                           workspace_root, source_root, snapshot["context"]["dataset_id"])
    captured_hash = evidence.get("sha256")
    result["current_source_matches"] = (current.get("sha256") == captured_hash) if captured_hash else None
    result["verification"] = ("file_unchanged" if result["current_source_matches"] else "file_changed") if captured_hash else "not_applicable"
    result["current_source_status"] = current["status"]
    return result


def chart_data(snapshot):
    r = snapshot["result"]
    return {"analysis_id": snapshot["analysis_id"], "revision": snapshot["revision"],
            "currency": r.get("currency"), "objects": [{key: row.get(key) for key in
             ("object_id", "name", "total_cost", "revenue", "operating_margin", "unit_cost", "quantity_unit")}
             for row in r.get("objects", [])], "cost_structure": copy.deepcopy(r["totals"].get("cost_structure", {}))}


def capture_legacy_evidence(export_facts, source_root, workspace_root):
    """Freeze legacy report sources at generation, with exact rows and fact IDs."""
    refs = {_json(ref): ref for ref in _refs(export_facts)}
    extra = {}
    dataset_id = next(iter(refs.values()), {}).get("dataset_id", "pharma-sample-v1")
    facts = export_facts.get("facts", {})
    source_cache = {}
    def periods_of(key):
        return {dict(parse_qsl(ref["selector"])).get("月份") for ref in _refs(facts.get(key, {}))
                if dict(parse_qsl(ref["selector"])).get("月份")}
    current_periods, previous_periods = periods_of("本月单位成本"), periods_of("上月单位成本")
    aggregate = len(current_periods) > 1
    raw_root = (Path(source_root) / "raw").resolve()
    # Older labor/manufacturing facts carry coordinates but not SourceRefs.
    # Resolve those coordinates against exact CSV cells and freeze each row.
    for name, fact in facts.items():
        if list(_refs(fact)) or not fact.get("source_file"):
            continue
        uri = "raw/" + str(fact["source_file"]).removeprefix("raw/")
        path = _inside(raw_root, Path(source_root) / uri)
        if not path.is_file() and Path(fact["source_file"]).name == fact["source_file"]:
            candidates = [p for p in raw_root.rglob(fact["source_file"]) if p.is_file() and not p.is_symlink()
                          and p.resolve().is_relative_to(raw_root)]
            if len(candidates) == 1:
                path = candidates[0].resolve()
                uri = "raw/" + path.relative_to(raw_root).as_posix()
        if not path.is_file() or path.suffix.lower() != ".csv":
            continue
        if path not in source_cache:
            data = path.read_bytes()
            source_cache[path] = {"data": data, "sha256": _sha(data), "parsed": {}}
        cached = source_cache[path]
        _csv_rows(cached["data"], {"selector": ""}, cached["parsed"])
        parts = fact.get("coord", "").strip("[]").split(":", 2)
        if len(parts) != 3:
            continue
        columns = [key for key in parts[1].split(",") if key in cached["parsed"]["headers"]]
        tokens = [x.strip() for x in parts[2].split("&") if x.strip()]
        months = {x for x in tokens if re.fullmatch(r"\d{4}-\d{2}", x)}
        if aggregate:
            months = (current_periods | previous_periods if "环比" in name else
                      previous_periods if "上月" in name else current_periods)
        entities = [x for x in tokens if not re.fullmatch(r"\d{4}-\d{2}", x)]
        if export_facts.get("product") and export_facts["product"] not in entities:
            entities.append(export_facts["product"])
        for record, row in cached["parsed"]["rows"].items():
            values = {str(v).strip() for v in row.values()}
            if any(entity not in values for entity in entities):
                continue
            if months and str(row.get("月份", "")).strip() not in months:
                continue
            for column in columns:
                ref = {"dataset_id": dataset_id, "source_uri": uri, "field": column,
                       "selector": urlencode({"row": record, "产品名称": export_facts.get("product", ""),
                                              "月份": row.get("月份", "")}),
                       "kind": "raw", "formula": fact.get("formula") or ""}
                refs[_json(ref)] = ref
                extra.setdefault(name, []).append(ref)
    evidence = [_capture_ref(ref, workspace_root, source_root, ref["dataset_id"], source_cache)
                for _, ref in sorted(refs.items())]
    by_ref = {_json({k: e.get(k, "") for k in ("dataset_id", "source_uri", "field", "selector", "kind", "formula")}):
              e["evidence_id"] for e in evidence}
    # Freeze the source version before adding purely presentational aliases.
    # Original IDs, values, selectors and hashes remain available in JSON.
    revision = _sha({"facts": export_facts, "evidence": evidence})
    quarterly = export_facts.get("period_kind") == "quarterly"
    current = str(export_facts.get("month") or "")

    def shift_month(value, delta):
        match = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if not match or not 1 <= int(match[2]) <= 12:
            return ""
        year, month = divmod(int(match[1]) * 12 + int(match[2]) - 1 + delta, 12)
        return f"{year:04d}-{month + 1:02d}"

    previous = str(export_facts.get("prev_month") or shift_month(current, -1))
    year_ago = str(export_facts.get("year_ago_month") or shift_month(current, -12))
    aliases = {current: current, previous: previous, year_ago: year_ago}
    windows = {}
    if quarterly:
        quarter = str(export_facts.get("period_label") or "")
        match = re.fullmatch(r"(\d{4})-Q([1-4])", quarter)
        if not match:
            raise ValueError("季度冻结事实缺少有效的 period_label，不能用月份别名标注季度")
        year, number = int(match[1]), int(match[2])
        previous_quarter = f"{year - 1}-Q4" if number == 1 else f"{year}-Q{number - 1}"
        year_ago_quarter = f"{year - 1}-Q{number}"
        aliases = {current: quarter, previous: previous_quarter, year_ago: year_ago_quarter}
        months = tuple(export_facts.get("input_periods") or
                       [f"{year:04d}-{month:02d}" for month in range(number * 3 - 2, number * 3 + 1)])
        windows = {quarter: months,
                   previous_quarter: tuple(shift_month(month, -3) for month in months),
                   year_ago_quarter: tuple(shift_month(month, -12) for month in months)}

    def shown_name(name):
        return (name.replace("去年同月", "去年同季").replace("本月", "本季度").replace("上月", "上季度")
                if quarterly else name)

    def shown_period(name, original):
        if name.startswith("去年"):
            return aliases.get(year_ago, year_ago)
        if name.startswith("上月") or name.endswith("上月"):
            return aliases.get(previous, previous)
        if quarterly:
            return aliases.get(str(original), str(original)) if original else aliases[current]
        return str(original or current)

    def value_unit(value, unit):
        if value is None or str(value).strip() in {"", "—", "未知"}:
            return "未知" if value is None or str(value).strip() == "" else str(value)
        value, unit = str(value).strip(), str(unit or "").strip()
        return value if not unit or value.endswith(unit) else f"{value} {unit}"

    evidence_aliases = {ev["evidence_id"]: f"E{index:04d}" for index, ev in enumerate(evidence, 1)}

    def compact_ids(ids):
        # Inclusive ranges shorten wide table cells without dropping references.
        numbers = sorted(int(evidence_aliases[identity][1:]) for identity in ids)
        ranges = []
        for number in numbers:
            if ranges and number == ranges[-1][1] + 1:
                ranges[-1][1] = number
            else:
                ranges.append([number, number])
        return "、".join(f"E{first:04d}" if first == last else f"E{first:04d}–E{last:04d}"
                        for first, last in ranges)

    bindings = []
    derived_formulas = {}
    derived_inputs = {}
    for index, (name, fact) in enumerate(export_facts.get("facts", {}).items(), 1):
        fact_refs = list(_refs(fact)) + extra.get(name, [])
        ids = sorted({by_ref[_json(ref)] for ref in fact_refs if _json(ref) in by_ref})
        input_periods = sorted({dict(parse_qsl(ref.get("selector", ""))).get("月份") for ref in fact_refs
                                if dict(parse_qsl(ref.get("selector", ""))).get("月份")})
        for ref in _refs(fact):
            # Core sources may be reused by ratios, shares and contributions;
            # those downstream formulas must not replace the source's formula.
            # A pharma-detail reference is dedicated to its same-named fact.
            if (ref.get("source_uri", "").startswith("derived:pharma-detail:")
                    and ref.get("field") == name and _json(ref) in by_ref):
                if fact.get("formula"):
                    derived_formulas.setdefault(by_ref[_json(ref)], set()).add(str(fact["formula"]))
                derived_inputs.setdefault(by_ref[_json(ref)], set()).update(input_periods)
        period = shown_period(name, fact.get("period", ""))
        bindings.append({"fact_id": fact.get("core_fact_id") or f"legacy-F{index:04d}",
                         "display_id": f"F{index:04d}", "name": shown_name(name), "original_name": name,
                         "value": fact.get("display", fact.get("value")), "unit": fact.get("unit", ""),
                         "value_unit": value_unit(fact.get("display", fact.get("value")), fact.get("unit", "")),
                         "period": period, "original_period": fact.get("period", ""),
                         "input_periods": input_periods or list(windows.get(period, (period,) if period else ())),
                         "source_ids": ids, "source_display_ids": sorted(evidence_aliases[identity] for identity in ids),
                         "legacy_coordinate": fact.get("coord", ""), "formula": fact.get("formula")})

    files = {}
    evidence_rows = []
    status_labels = {"captured": "已冻结", "derived": "派生，非原始行", "synthetic": "模拟数据",
                     "missing": "文件缺失", "selector_not_found": "未找到记录", "field_not_found": "列不存在",
                     "selector_field_not_found": "筛选列不存在", "reference_only": "仅来源标识"}
    for ev in evidence:
        ev["display_id"] = evidence_aliases[ev["evidence_id"]]
        raw_rows = ev.get("rows", [])
        if ev["status"] == "derived":
            selected = dict(parse_qsl(ev.get("selector", "")))
            internal_period = selected.get("synthetic_join_label") or ev["source_uri"].rsplit("@", 1)[-1]
            period = aliases.get(internal_period, "") if quarterly else internal_period
            input_months = sorted(derived_inputs.get(ev["evidence_id"], ())) or list(windows.get(period, ()))
            if not input_months:
                input_months = sorted(set(re.findall(r"\d{4}-(?:0[1-9]|1[0-2])", ev.get("selector", ""))))
            ev["display_period"] = period
            ev["display_input_periods"] = input_months
            ev["display_field"] = shown_name(ev["field"])
            formula = "；".join(sorted(derived_formulas.get(ev["evidence_id"], ()))) or ev.get("formula", "")
            ev["display_formula"] = formula
            detail = "输入月份：" + "、".join(input_months) if input_months else "输入范围见同一事实的原始证据"
            detail += "；算式：" + formula if formula else "；算式未提供，待复核"
            evidence_rows.append([ev["display_id"], period or "期间待复核", ev["display_field"], detail,
                                  status_labels["derived"]])
            continue
        file_key = (ev.get("dataset_id", ""), ev["source_uri"], ev.get("sha256", ""))
        if file_key not in files:
            files[file_key] = {"display_id": f"S{len(files) + 1:03d}", "dataset_id": file_key[0],
                               "source_uri": file_key[1], "sha256": file_key[2]}
        ev["file_display_id"] = files[file_key]["display_id"]
        row_periods = sorted({str(row.get("values", {}).get("月份", "")) for row in raw_rows
                              if row.get("values", {}).get("月份")})
        ev["display_period"] = "、".join(row_periods)
        location = ev["file_display_id"] + ("；" + ev["display_period"] if row_periods else "")
        record_values = "；".join(f"CSV记录{row['record']}：{_cell(row.get('value'))}" for row in raw_rows)
        detail = record_values or ev.get("selector") or ev.get("description", "来源待复核")
        evidence_rows.append([ev["display_id"], location, ev["field"], detail,
                              status_labels.get(ev["status"], ev["status"])])

    def grouped_hash(value):
        return " ".join(value[index:index + 16] for index in range(0, len(value), 16)) if value else "未取得哈希"

    lines = ["", "## 附录 核算事实与来源复核", "",
             "下列事实与图表数据在报告生成时冻结。CSV记录序号从表头1开始，数据从记录2开始。",
             "F为本报告事实编号，E为证据编号，S为来源文件版本编号；E编号范围包含首尾。完整原始编号与对应关系保留在报告来源JSON中。",
             "来源版本：" + revision, ""]
    if quarterly:
        lines += [f"本季度：{aliases[current]}（{'、'.join(windows[aliases[current]])}）；"
                  f"上季度：{aliases[previous]}（{'、'.join(windows[aliases[previous]])}）；"
                  f"去年同季：{aliases[year_ago]}（{'、'.join(windows[aliases[year_ago]])}）。"
                  "季度指标按真实季度标注；原始记录索引保留各输入月份。", ""]
    lines += _table(["事实编号", "指标", "期间", "值/单位", "证据编号"], [
        [x["display_id"], x["name"], x["period"], x["value_unit"], compact_ids(x["source_ids"]) or
         ("坐标待补全：" + x["legacy_coordinate"])] for x in bindings])
    lines += ["", "### 来源文件版本", "",
              "SHA-256按16字符分组便于阅读，核对时去除分组空格。每个文件版本只列一次，原始完整哈希保留在JSON中。", ""]
    lines += _table(["文件编号", "来源路径", "SHA-256"], [
        [item["display_id"], item["source_uri"], grouped_hash(item["sha256"])] for item in files.values()])
    lines += ["", "### 原始记录索引", ""]
    lines += _table(["证据编号", "文件/期间", "列/指标", "原始行值或计算依据", "状态"], evidence_rows)
    return {"source_revision": revision, "evidence": evidence, "fact_bindings": bindings,
            "file_index": list(files.values()), "markdown": "\n".join(lines)}


def answer_snapshot(snapshot, question):
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError("问题须为1—2000字的文本")
    r, c = snapshot["result"], snapshot["context"]
    scope = f"{c.get('dataset_label', c['dataset_id'])} / {c['period']} / {c['scenario']}"
    requested = set(re.findall(r"(?<!\d)(\d{4}-(?:Q[1-4]|\d{2}))(?!\d)", question))
    requested.update(f"{year}-{int(month):02d}" for year, month in re.findall(r"(\d{4})年(\d{1,2})月", question))
    if requested and requested != {c["period"]}:
        return {"answer": f"当前分析：{scope}。问题涉及不同期间；本次快照不含所需期间，不能用当前数值替代。请切换期间重新生成分析。\n结果版本：{snapshot['revision']}。",
                "analysis_id": snapshot["analysis_id"], "revision": snapshot["revision"],
                "context": c, "evidence_ids": [], "mode": "deterministic_snapshot", "model_calls": 0}
    matches = [row for row in r.get("objects", []) if row["name"] in question or row["object_id"] in question]
    rows = matches or r.get("objects", [])
    lines = [f"当前分析：{scope}。"]
    if not matches and any(word in question for word in ("总收入", "总成本", "合计", "汇总", "整体")):
        totals = r["totals"]
        lines += [f"当前范围合计：总成本 {_number(totals.get('total_cost'))}，总收入 {_number(totals.get('total_revenue'))}，经营毛差 {_number(totals.get('total_operating_margin'))} {r['currency']}。"]
    if any(word in question for word in ("为什么", "原因", "归因", "设备", "故障")):
        lines += ["当前快照包含核算与分摊结果，未提供足以证明该业务原因的证据，不能作确定归因。"]
    if any(word in question for word in ("收入", "利润", "亏损", "盈利", "毛差")):
        lines += [f"{row['name']}：收入 {_number(row.get('revenue'))}，经营毛差 {_number(row.get('operating_margin'))} {r['currency']}。" for row in rows]
        if any(row.get("revenue") is None for row in rows if row.get("kind") != "cost_center"):
            lines += ["缺少收入数据的对象无法判断盈利或亏损。"]
    elif any(word in question for word in ("结构", "类别", "构成")):
        for row in rows:
            lines += [f"{row['name']}：" + "；".join(f"{k} {_number(v)} {r['currency']}" for k, v in row.get("cost_structure", {}).items()) + "。"]
    elif any(word in question for word in ("预算", "标准", "差异")):
        wanted = {x["object_id"] for x in rows}
        items = [x for x in r.get("benchmarks", []) + r.get("standard_variances", []) if x.get("object_id") in wanted]
        lines += [_json(x) for x in items] or ["当前范围没有可用的预算或标准差异数据。"]
    else:
        lines += [f"{row['name']}：总成本 {_number(row.get('total_cost'))} {r['currency']}；单位成本 {_number(row.get('unit_cost'), 4)} {r['currency']}/{row.get('quantity_unit', '')}。" for row in rows]
    if any(word in question for word in ("质量", "异常", "缺失")):
        lines += [str(x.get("message") or x.get("code")) for x in r.get("quality", [])]
    evidence_ids = sorted({eid for row in rows for eid in _source_ids(snapshot, row)})
    lines += [f"结果版本：{snapshot['revision']}。"]
    return {"answer": "\n".join(lines), "analysis_id": snapshot["analysis_id"], "revision": snapshot["revision"],
            "context": c, "evidence_ids": evidence_ids, "mode": "deterministic_snapshot", "model_calls": 0}


def _charts(snapshot, directory):
    from chart_render import _setup_font
    _setup_font()
    from matplotlib.figure import Figure
    items = chart_data(snapshot)
    charts = []
    for filename, data, title in (
        ("objects.png", [(x["name"], x["total_cost"]) for x in items["objects"]], "成本对象总成本"),
        ("structure.png", list(items["cost_structure"].items()), "成本类别构成"),
    ):
        if not data:
            continue
        fig = Figure(figsize=(8, max(2.8, len(data) * .34)), dpi=160)
        ax = fig.subplots()
        positions = list(range(len(data)))
        ax.barh(positions, [float(Decimal(str(v))) for _, v in data], color="#0f766e")
        ax.set_yticks(positions, [str(k) for k, _ in data])
        ax.invert_yaxis()
        ax.set_xlabel(str(items["currency"]))
        ax.set_title(title)
        fig.tight_layout()
        target = directory / filename
        fig.savefig(target)
        charts.append({"path": target, "image": target, "caption": f"{title}（与分析快照一致）", "after": "## 三、成本对象与成本结构"})
    return charts


def export_snapshot(snapshot, fmt, output_dir):
    fmt = str(fmt).lower()
    if fmt not in {"docx", "pdf", "md", "json"}:
        raise ValueError("导出格式只能是docx、pdf、md或json")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Each export is isolated; LaTeX intermediates cannot race across users or formats.
    directory = Path(tempfile.mkdtemp(prefix=snapshot["analysis_id"] + "-", dir=output_dir))
    stem = "成本报告_" + re.sub(r"[^0-9A-Za-z._-]", "_", str(snapshot["context"]["period"])) + "_" + snapshot["analysis_id"][:8]
    target = directory / (stem + "." + fmt)
    if fmt == "json":
        target.write_text(_json(snapshot), encoding="utf-8")
        return target
    markdown = snapshot["report"]["markdown"]
    md_path = directory / (stem + ".md")
    md_path.write_text(markdown, encoding="utf-8")
    if fmt == "md":
        return md_path
    charts = _charts(snapshot, directory)
    title = markdown.splitlines()[0].lstrip("# ")
    if fmt == "docx":
        from export_doc import to_docx
        to_docx(md_path, target, title=title, charts=charts)
        # Formal body defaults: A4, 2cm margins, 12pt, Chinese Song/Latin Times.
        from docx import Document
        from docx.shared import Cm, Pt
        from docx.oxml.ns import qn
        doc = Document(target)
        for section in doc.sections:
            section.page_width, section.page_height = Cm(21), Cm(29.7)
            section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Cm(2)
        for paragraph in doc.paragraphs:
            style_name = paragraph.style.name
            if style_name not in {"Chart Image", "Chart Caption", "Evidence Text"}:
                paragraph.paragraph_format.line_spacing = 1
            if style_name == "Normal" and paragraph.text.strip():
                paragraph.paragraph_format.first_line_indent = Pt(24)
            for run in paragraph.runs:
                run.font.name = "Times New Roman"
                run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
                if style_name in {"Normal", "List Bullet"}:
                    run.font.size = Pt(12)
        doc.save(target)
        return target
    pandoc = shutil.which("pandoc")
    if not pandoc or not shutil.which("xelatex"):
        raise RuntimeError("PDF导出需要pandoc和xelatex；当前镜像缺少已声明的依赖")
    # The same two-stage safe reader as legacy exports neutralizes input TeX,
    # HTML, raw attributes, math, metadata and original images before adding
    # program-generated charts. CSV/evidence labels never gain TeX privileges.
    from export_doc import to_pdf
    font = "SimSun" if Path("/usr/local/share/fonts/windows/simsun.ttc").exists() else "Noto Serif CJK SC"
    latin = "Times New Roman" if Path("/usr/local/share/fonts/windows/times.ttf").exists() else "Latin Modern Roman"
    header = ("\\usepackage{xeCJK}\n\\setCJKmainfont{" + font + "}\n"
              "\\setmainfont{" + latin + "}\n\\usepackage{graphicx,xcolor,fancyhdr}\n"
              "\\pagestyle{fancy}\n\\fancyhf{}\n\\fancyfoot[C]{\\thepage}\n"
              "\\setlength{\\parindent}{2em}\n\\setlength{\\emergencystretch}{3em}\n")
    return to_pdf(md_path, target, charts=charts, latex_header=header,
                  variables=["geometry:a4paper,margin=2cm", "fontsize=12pt", "linestretch=1", "colorlinks=true"],
                  shift_heading_level_by=0, timeout=180)
