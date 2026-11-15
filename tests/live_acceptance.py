#!/usr/bin/env python3
"""Real HTTP acceptance against an explicitly selected running deployment.

Creates two clearly named validation accounts and fixture CSV datasets. Passwords
and session cookies exist only in memory. It never dispatches RPA tasks and every
authenticated legacy generation explicitly sets use_llm=False. Existing user
files/accounts are neither changed nor removed.

Example inside the application container:
  python tests/live_acceptance.py --base-url http://127.0.0.1:8765 \
    --output-dir /data/acceptance --data-root /data

Every run receives a new output subdirectory. DOCX/PDF checks validate structure
and expected text; visual layout remains a separate rendered-page review.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

import httpx


FIXTURE_CSV = (
    "object_id,object_name,quantity,quantity_unit,currency,cost_element,amount,behavior,selling_price,period\n"
    "P1,验收产品A,10,件,CNY,材料,100,variable,20,2026-05\n"
    "P1,验收产品A,10,件,CNY,人工,50,fixed,20,2026-05\n"
    "P2,验收产品B,3,件,CNY,材料,60,variable,,2026-05\n"
)
FIXTURE_SCHEMA = {"label": "HTTP验收CSV", "industry": "验收夹具", "period": "2026-05",
                  "scenario": "actual", "currency": "CNY", "allocation_rules": []}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def amount(value):
    return None if value is None else Decimal(str(value))


class Acceptance:
    def __init__(self, base_url: str, output_dir: Path, data_root: Path | None = None):
        self.base_url = base_url.rstrip("/")
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        self.output = Path(output_dir).resolve() / ("live-" + self.run_id)
        self.output.mkdir(parents=True, exist_ok=False)
        self.artifact_dir = self.output / "artifacts"
        self.artifact_dir.mkdir()
        self.data_root = Path(data_root).resolve() if data_root is not None else None
        self.clients = {name: httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(240, connect=15),
                                          trust_env=False, follow_redirects=False)
                        for name in ("anonymous", "alice", "bob")}
        self.secrets = set()
        self.account_names = {}
        self.report = {"run_id": self.run_id, "base_url": self.base_url,
                       "started_at": datetime.now(timezone.utc).isoformat(),
                       "checks": [], "http": [], "artifacts": [], "accounts": {},
                       "scope": {"real_http": True, "authenticated_model_requests": 0,
                                 "rpa_dispatch_requests": 0, "credentials_persisted": False,
                                 "physical_bad_import_check": data_root is not None,
                                 "visual_layout_verified": False},
                       "limitations": ["文件验证包含DOCX/PDF结构和关键文本；版面须另行渲染检查。"]}
        if data_root is None:
            self.report["limitations"].append("未给--data-root：坏导入只核对HTTP数据集、工作区和报告无新增；物理临时目录未验证。")
        self.flush()

    def clean(self, value):
        if isinstance(value, dict):
            return {str(key): self.clean(item) for key, item in value.items()
                    if str(key).lower() not in {"password", "token", "authorization", "cookie", "set-cookie", "csrf_token"}}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, str):
            for secret in self.secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        return value

    def flush(self):
        temporary = self.output / ".results.tmp"
        temporary.write_text(json.dumps(self.clean(self.report), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.output / "results.json")

    def step(self, name, action):
        started = time.monotonic()
        print(json.dumps({"event": "started", "check": name}, ensure_ascii=False), flush=True)
        try:
            value = action()
            record = {"name": name, "status": "passed", "seconds": round(time.monotonic() - started, 3)}
            if isinstance(value, dict):
                record["details"] = value
        except Exception as exc:
            value = None
            record = {"name": name, "status": "failed", "seconds": round(time.monotonic() - started, 3),
                      "error_type": type(exc).__name__, "error": str(exc)[:1500]}
        self.report["checks"].append(self.clean(record))
        self.flush()
        print(json.dumps(self.clean({"event": "finished", **{key: item for key, item in record.items() if key != "details"}}), ensure_ascii=False), flush=True)
        return value

    def blocked(self, name, reason):
        self.report["checks"].append({"name": name, "status": "blocked", "reason": reason})
        self.flush()

    def request(self, who, method, path, expected=200, *, csrf=True, **kwargs):
        client = self.clients[who]
        headers = dict(kwargs.pop("headers", {}))
        if csrf and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            csrf_value = client.cookies.get("lw_csrf")
            if csrf_value:
                headers["X-CSRF-Token"] = csrf_value
        start = time.monotonic()
        response = client.request(method, path, headers=headers, **kwargs)
        self.secrets.update(cookie.value for cookie in client.cookies.jar)
        self.report["http"].append({"account": who, "method": method.upper(), "path": path,
                                    "status": response.status_code,
                                    "seconds": round(time.monotonic() - start, 3),
                                    "expected": expected})
        acceptable = {expected} if isinstance(expected, int) else set(expected)
        if response.status_code not in acceptable:
            try:
                detail = json.dumps(self.clean(response.json()), ensure_ascii=False)[:1200]
            except Exception:
                detail = self.clean(response.text[:500])
            raise AssertionError(f"{method} {path}: HTTP {response.status_code}; expected {sorted(acceptable)}; {detail}")
        return response

    def get_json(self, who, path, **kwargs):
        return self.request(who, "GET", path, **kwargs).json()

    def write_json(self, name, value):
        target = self.artifact_dir / (name + ".json")
        with target.open("x", encoding="utf-8") as stream:
            json.dump(self.clean(value), stream, ensure_ascii=False, indent=2)
        self.report["artifacts"].append({"file": str(target), "kind": "json-evidence", "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "bytes": target.stat().st_size})
        return str(target)

    def preflight(self):
        health = self.get_json("anonymous", "/api/health")
        require(health.get("ok") is True, "服务健康检查未通过")
        schema = self.get_json("anonymous", "/openapi.json")
        endpoints = schema.get("paths", {})
        require("/api/platform/costing-snapshots/{analysis_id}/question" in endpoints, "当前服务不是本次修复版本：缺少快照问答接口")
        generate = schema.get("components", {}).get("schemas", {}).get("GenerateIn", {})
        require("use_llm" in generate.get("properties", {}), "当前服务不支持明确关闭模型；停止运行以避免误调模型")
        return {"health": health, "api_version": schema.get("info", {}).get("version")}

    def register(self, who):
        name = "ictv_" + secrets.token_hex(6) + ("_a" if who == "alice" else "_b")
        password = secrets.token_urlsafe(30)
        self.secrets.add(password)
        result = self.request(who, "POST", "/api/auth/register", json={"username": name, "password": password, "name": "HTTP独立验收"}).json()
        require(result.get("ok") is True, "验证账号未创建成功")
        me = self.get_json(who, "/api/auth/me")
        require(me.get("logged_in") is True and me.get("external_id") == name, "账号会话身份不匹配")
        self.account_names[who] = name
        self.report["accounts"][who] = {"external_id": name, "purpose": "isolated_acceptance_fixture"}
        return {"external_id": name, "logged_in": True}

    def upload(self, schema=None, who="alice", expected=200, csrf=True):
        return self.request(who, "POST", "/api/platform/costing-import", expected, csrf=csrf,
                            files={"file": ("http_fixture.csv", FIXTURE_CSV.encode("utf-8"), "text/csv")},
                            data={"schema_json": json.dumps(FIXTURE_SCHEMA if schema is None else schema, ensure_ascii=False),
                                  "label": "HTTP验收-" + self.run_id})

    def create_csv(self):
        data = self.upload().json()
        snapshot = data["analysis"]
        objects = {item["object_id"]: item for item in snapshot["result"]["objects"]}
        require(amount(objects["P1"]["total_cost"]) == Decimal("150"), "P1成本必须为150")
        require(amount(objects["P1"]["unit_cost"]) == Decimal("15"), "P1单位成本必须为15")
        require(amount(objects["P1"]["revenue"]) == Decimal("200"), "P1收入必须为200")
        require(amount(objects["P1"]["operating_margin"]) == Decimal("50"), "P1经营毛差必须为50")
        require(amount(objects["P2"]["unit_cost"]) == Decimal("20"), "P2单位成本必须为20")
        require(objects["P2"]["revenue"] is None and objects["P2"]["operating_margin"] is None, "缺收入的P2必须保留未知")
        require(snapshot["result"]["totals"]["total_revenue"] is None, "部分收入不可伪装成总收入")
        require(snapshot["result"]["totals"]["total_operating_margin"] is None, "部分收入不可给整体盈亏结论")
        require(snapshot["context"]["owner_external_id"] == self.account_names["alice"], "CSV快照的所有者错误")
        self.write_json("csv_import", data)
        self.csv_dataset = data["dataset"]["dataset_id"]
        self.csv_snapshot = snapshot
        return {"dataset_id": self.csv_dataset, "analysis_id": snapshot["analysis_id"], "revision": snapshot["revision"],
                "business_values": {"P1_total": "150", "P1_unit": "15", "P1_revenue": "200", "P1_margin": "50", "P2_unit": "20", "aggregate_income": None}}

    def fingerprint(self):
        listing = self.get_json("alice", "/api/platform/costing-industries")
        workspace = self.get_json("alice", "/api/control/workspace")
        reports = self.get_json("alice", "/api/control/exports")
        result = {"datasets": sorted(item["dataset_id"] for item in listing.get("datasets", []) if item["dataset_id"].startswith("csv-")),
                  "raw": sorted((item["path"], item["size"]) for item in workspace.get("raw", [])),
                  "reports": sorted(item["name"] for item in reports.get("reports", []) if item.get("scope") == "personal")}
        if self.data_root is not None:
            folder = self.data_root / "users" / self.account_names["alice"] / "ws" / "costing_datasets"
            require(folder.is_dir(), "--data-root与服务端数据根不符：找不到本次验证账户的数据集目录")
            entries = []
            for item in sorted(folder.rglob("*")):
                require(not item.is_symlink(), "验收账户数据集目录出现符号链接")
                entries.append((item.relative_to(folder).as_posix(), "dir" if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest()))
            result["physical_dataset_tree"] = entries
        return result

    def malformed_imports(self):
        before = self.fingerprint()
        schemas = {"columns_number": {**FIXTURE_SCHEMA, "columns": 123},
                   "columns_nested_list": {**FIXTURE_SCHEMA, "columns": {"amount": []}},
                   "allocation_number": {**FIXTURE_SCHEMA, "allocation_rules": 123},
                   "currency_missing_fields": {**FIXTURE_SCHEMA, "currency_rates": [{}]},
                   "unit_conversion_missing_fields": {**FIXTURE_SCHEMA, "unit_conversions": [{}]},
                   "client_plugin_override": {**FIXTURE_SCHEMA, "plugin_id": "not-allowed"}}
        statuses = {}
        for name, schema in schemas.items():
            response = self.upload(schema, expected=422)
            statuses[name] = response.status_code
            require(self.fingerprint() == before, f"坏导入{name}留下了新增数据或改变了既有数据")
        return {"statuses": statuses, "before_equals_after": True,
                "physical_check": "passed" if self.data_root is not None else "unverified",
                "fingerprint": before}

    def negative_access(self):
        snapshot = self.csv_snapshot
        ident, dataset = snapshot["analysis_id"], self.csv_dataset
        path = "/api/platform/costing-snapshots/" + ident
        statuses = {}
        for suffix in ("", "/charts", "/export?format=json"):
            statuses["bob" + suffix] = self.request("bob", "GET", path + suffix, 404).status_code
        self.request("bob", "POST", path + "/question", 404, json={"question": "此结果是什么？"})
        self.request("bob", "POST", path + "/report", 404)
        evidence_id = snapshot["evidence"][0]["evidence_id"]
        self.request("bob", "GET", path + "/evidence/" + evidence_id, 404)
        self.request("anonymous", "GET", path, 401)
        self.request("bob", "GET", "/api/platform/costing-analysis", 404, params={"dataset_id": dataset, "period": "2026-05"})
        self.request("anonymous", "GET", "/api/platform/costing-analysis", 401, params={"dataset_id": dataset, "period": "2026-05"})
        self.upload(who="anonymous", expected=401)
        self.upload(who="alice", expected=403, csrf=False)
        self.request("alice", "POST", path + "/report", 403, csrf=False)
        self.request("alice", "DELETE", "/api/conversations/99999999", 403, csrf=False)
        bob_ids = {item["dataset_id"] for item in self.get_json("bob", "/api/platform/costing-industries")["datasets"]}
        require(dataset not in bob_ids, "B账户列表出现A账户数据集")
        return {"cross_account": "rejected", "anonymous_private": "rejected", "missing_csrf": "rejected", "statuses": statuses}

    def anonymous_model_guards(self):
        statuses = {}
        statuses["route"] = self.request("anonymous", "POST", "/api/route", 401, json={"question": "匿名验收请求，不应执行"}).status_code
        products = self.get_json("anonymous", "/api/products").get("products", [])
        product = products[0] if products else "未授权测试对象"
        statuses["benchmark"] = self.request("anonymous", "GET", "/api/benchmark", 401, params={"product": product, "month": "2026-05", "llm": "1"}).status_code
        statuses["chat"] = self.request("anonymous", "POST", "/api/chat", 401, json={"question": "匿名验收请求，不应执行"}).status_code
        statuses["generate"] = self.request("anonymous", "POST", "/api/control/generate", 401, json={"product": product, "use_llm": False}).status_code
        statuses["ingest"] = self.request("anonymous", "POST", "/api/control/ingest", 401).status_code
        return {"all_rejected_before_execution": True, "statuses": statuses}

    def snapshot_flow(self, label, snapshot, *, reload_dataset=False):
        ident, revision, context = snapshot["analysis_id"], snapshot["revision"], snapshot["context"]
        prefix = "/api/platform/costing-snapshots/" + ident
        if reload_dataset:
            fresh = self.get_json("alice", "/api/platform/costing-analysis", params={"dataset_id": context["dataset_id"], "period": context["period"], "scenario": context["scenario"]})
            require(fresh["revision"] == revision, "重载同一数据集改变了数据与计算版本")
        answer = self.request("alice", "POST", prefix + "/question", json={"question": "收入、利润和缺失数据是什么？"}).json()
        require(answer.get("revision") == revision and answer.get("context") == context, "问答未绑定相同快照上下文")
        require(answer.get("model_calls") == 0, "快照问答调用了模型或未明确报告零调用")
        charts = self.get_json("alice", prefix + "/charts")
        require(charts["revision"] == revision and charts["context"] == context, "图表未绑定相同快照")
        chart_amounts = {item["object_id"]: amount(item["total_cost"]) for item in charts["charts"]["objects"]}
        expected_amounts = {item["object_id"]: amount(item["total_cost"]) for item in snapshot["result"]["objects"]}
        require(chart_amounts == expected_amounts, "图表金额与核算结果不一致")
        saved = self.request("alice", "POST", prefix + "/report").json()
        require(saved["analysis_id"] == ident and saved["revision"] == revision, "保存的报告版本不一致")
        reopened = self.get_json("alice", prefix)
        require(reopened["revision"] == revision and reopened["result"] == snapshot["result"], "重开分析改变了结果")
        source_counts = Counter(item.get("status", "unknown") for item in snapshot.get("evidence", []))
        require(not source_counts["missing"] and not source_counts["selector_not_found"], "快照存在丢失来源或未匹配原始行")
        evidence = next((item for item in snapshot.get("evidence", []) if item.get("status") == "captured"), None)
        if evidence is None:
            evidence = next(iter(snapshot.get("evidence", [])), None)
        require(evidence is not None, "快照没有来源证据")
        detail = self.get_json("alice", prefix + "/evidence/" + evidence["evidence_id"])
        require(detail.get("revision") == revision and detail.get("context") == context, "来源不是该次分析的版本")
        require(detail.get("current_source_matches") is not False, "生成之后原始来源已变化")
        if context["dataset_id"].startswith("csv-"):
            require(detail.get("rows"), "CSV来源没有实际原始行")
        listed = self.get_json("alice", "/api/control/exports")["reports"]
        require(any(item.get("analysis_id") == ident and item["name"] == saved["file"] for item in listed), "已保存报告没有出现在当前账户清单")
        self.write_json(label + "_flow", {"analysis_id": ident, "revision": revision, "context": context,
                                           "answer": answer, "charts": charts, "saved": saved, "evidence_detail": detail,
                                           "evidence_statuses": dict(source_counts)})
        return {"analysis_id": ident, "revision": revision, "source_statuses": dict(source_counts), "question_model_calls": 0}

    def download(self, label, fmt, response, expected_tokens, snapshot=None):
        body = response.content
        require(0 < len(body) <= 100 * 1024 * 1024, "导出为空或超过100MB验收上限")
        validation = {"format": fmt, "bytes": len(body), "content_type": response.headers.get("content-type", "")}
        if fmt == "json":
            value = response.json()
            require(snapshot is not None and value["revision"] == snapshot["revision"] and value["result"] == snapshot["result"], "导出JSON与原快照不一致")
            validation["same_snapshot"] = True
        elif fmt == "docx":
            require(body.startswith(b"PK"), "Word导出不是DOCX压缩包")
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                require("word/document.xml" in archive.namelist(), "Word文档主体缺失")
                document = ET.fromstring(archive.read("word/document.xml"))
                text = "".join(document.itertext())
                validation["images"] = sum(name.startswith("word/media/") for name in archive.namelist())
                validation["tables"] = len(document.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tbl"))
                require(validation["images"] > 0, "Word没有嵌入当前分析图表")
                require(validation["tables"] > 0, "Word没有核算表格")
        elif fmt == "pdf":
            require(body.startswith(b"%PDF-"), "PDF导出不是PDF文件")
            import fitz
            with fitz.open(stream=body, filetype="pdf") as document:
                require(document.page_count > 0 and not document.is_encrypted, "PDF无页面或无法读取")
                text = "\n".join(page.get_text() for page in document)
                validation["pages"] = document.page_count
                validation["page_sizes"] = sorted({(round(page.rect.width, 1), round(page.rect.height, 1)) for page in document})
        else:
            raise AssertionError("未知验收导出格式")
        if fmt != "json":
            normalized = re.sub(r"\s+", "", text)
            missing = [token for token in expected_tokens if re.sub(r"\s+", "", str(token)) not in normalized]
            require(not missing, "导出正文缺少预期范围/版本：" + ", ".join(missing))
            validation["expected_text_found"] = list(expected_tokens)
        target = self.artifact_dir / (label + "." + fmt)
        with target.open("xb") as stream:
            stream.write(body)
        artifact = {"file": str(target), "kind": "download", "sha256": hashlib.sha256(body).hexdigest(), **validation}
        self.report["artifacts"].append(artifact)
        return artifact

    def export_snapshot(self, label, snapshot, fmt):
        path = "/api/platform/costing-snapshots/" + snapshot["analysis_id"] + "/export"
        response = self.request("alice", "GET", path, params={"format": fmt})
        return self.download(label, fmt, response, [snapshot["context"]["dataset_id"], snapshot["context"]["period"], snapshot["analysis_id"]], snapshot)

    def sample(self, dataset):
        scenario = "actual" if "actual" in dataset.get("scenarios", ["actual"]) else dataset["scenarios"][0]
        periods = dataset.get("periods_by_scenario", {}).get(scenario) or dataset.get("periods", [])
        require(periods, "数据集没有可用期间：" + dataset["dataset_id"])
        period = "2026-05" if "2026-05" in periods else periods[-1]
        snapshot = self.get_json("alice", "/api/platform/costing-analysis", params={"dataset_id": dataset["dataset_id"], "period": period, "scenario": scenario})
        require(snapshot["context"]["dataset_id"] == dataset["dataset_id"], "行业数据集作用域错误")
        require(snapshot["result"]["objects"], "行业分析没有成本对象")
        if dataset["dataset_id"] == "pharma-sample-v1":
            require(snapshot["result"]["totals"]["total_revenue"] is None, "药企缺收入却返回了总收入")
            require(snapshot["result"]["totals"]["total_operating_margin"] is None, "药企缺收入却给出了经营盈亏")
        self.write_json(dataset["dataset_id"] + "_analysis", snapshot)
        self.sample_snapshots[dataset["dataset_id"]] = snapshot
        return {"dataset_id": dataset["dataset_id"], "analysis_id": snapshot["analysis_id"], "revision": snapshot["revision"],
                "period": period, "objects": len(snapshot["result"]["objects"]), "quality": snapshot["result"].get("quality", [])}

    def legacy(self, theme, month, product, period):
        response = self.request("alice", "POST", "/api/control/generate", json={"product": product, "month": month,
                                "theme": theme, "period": period, "dataset_id": "pharma-sample-v1", "use_llm": False}).json()
        require(response.get("stats", {}).get("llm_calls") == 0, "离线报告未明确证明零模型调用")
        metadata = response["metadata"]
        require(metadata["theme"] == theme and metadata["product"] == product, "报告的主题/产品元数据不一致")
        require(metadata["period"] == (period if theme == "quarterly" else month), "报告期间元数据不一致")
        require(metadata.get("has_frozen_facts") and metadata.get("source_revision"), "报告未冻结核算事实或来源版本")
        name = response["file"]
        sources = self.get_json("alice", "/api/control/report-evidence", params={"report": name})
        require(sources.get("evidence"), "报告没有可核对来源")
        statuses = Counter(item.get("status", "unknown") for item in sources["evidence"])
        require(not statuses["missing"] and not statuses["selector_not_found"], "报告存在缺失来源或未匹配原始行")
        captured = next((item for item in sources["evidence"] if item.get("status") == "captured"), None)
        require(captured is not None, "报告没有冻结原始记录")
        detail = self.get_json("alice", "/api/control/report-evidence", params={"report": name, "evidence_id": captured["evidence_id"]})
        require(detail.get("evidence", {}).get("rows"), "报告证据未返回原始CSV行列")
        self.request("bob", "GET", "/api/control/report-evidence", 404, params={"report": name})
        self.request("bob", "POST", "/api/control/export", 404, json={"md": name, "fmt": "docx"})
        self.write_json("legacy_" + theme, {"generated": response, "sources": sources, "source_detail": detail})
        self.legacy_reports[theme] = response
        return {"file": name, "metadata": metadata, "llm_calls": 0, "source_statuses": dict(statuses)}

    def export_legacy(self, theme, fmt):
        report = self.legacy_reports[theme]
        response = self.request("alice", "POST", "/api/control/export", json={"md": report["file"], "fmt": fmt, "charts": True})
        return self.download("legacy_" + theme, fmt, response, [report["metadata"]["product"], report["metadata"]["source_revision"]])

    def run(self):
        self.sample_snapshots, self.legacy_reports = {}, {}
        if self.step("deployment_preflight", self.preflight) is None:
            self.blocked("remaining_http_acceptance", "部署版本或连接预检失败，停止后续修改请求")
            return self.finish()
        registered = [self.step("register_" + who, lambda who=who: self.register(who)) for who in ("alice", "bob")]
        self.step("anonymous_paid_entrypoints_rejected", self.anonymous_model_guards)
        if not all(registered):
            self.blocked("authenticated_acceptance", "两个隔离测试账号未就绪")
            return self.finish()
        created = self.step("csv_import_and_business_values", self.create_csv)
        if created:
            self.step("malformed_csv_rejected_without_artifacts", self.malformed_imports)
            self.step("cross_account_and_csrf_guards", self.negative_access)
            self.step("csv_same_snapshot_end_to_end", lambda: self.snapshot_flow("csv", self.csv_snapshot, reload_dataset=True))
            for fmt in ("docx", "pdf", "json"):
                self.step("csv_export_" + fmt, lambda fmt=fmt: self.export_snapshot("csv", self.csv_snapshot, fmt))
        else:
            self.blocked("csv_dependents", "CSV导入前置失败")
        listing = self.step("industry_metadata", lambda: self.get_json("alice", "/api/platform/costing-industries"))
        datasets = {item["dataset_id"]: item for item in (listing or {}).get("datasets", [])}
        for ident in ("restaurant-sample-v1", "manufacturing-sample-v1", "pharma-sample-v1"):
            if ident not in datasets:
                self.blocked(ident + "_analysis", "服务端未注册此数据集")
                continue
            analyzed = self.step(ident + "_analysis", lambda ident=ident: self.sample(datasets[ident]))
            if analyzed:
                snapshot = self.sample_snapshots[ident]
                self.step(ident + "_same_snapshot_flow", lambda ident=ident, snapshot=snapshot: self.snapshot_flow(ident, snapshot))
                for fmt in ("docx", "pdf", "json"):
                    self.step(ident + "_export_" + fmt, lambda ident=ident, snapshot=snapshot, fmt=fmt: self.export_snapshot(ident, snapshot, fmt))
        themes = self.step("legacy_theme_metadata", lambda: self.get_json("anonymous", "/api/themes"))
        products = self.step("legacy_products", lambda: self.get_json("anonymous", "/api/products"))
        pharma = datasets.get("pharma-sample-v1", {})
        actual_months = pharma.get("periods_by_scenario", {}).get("actual") or pharma.get("periods", [])
        product_list = (products or {}).get("products", [])
        if not actual_months or not product_list or themes is None:
            self.blocked("legacy_all_themes", "药企实际数据、产品或主题不可用")
        else:
            product = "银黄口服液" if "银黄口服液" in product_list else product_list[0]
            month = "2026-05" if "2026-05" in actual_months else actual_months[-1]
            theme_map = {item["id"]: item for item in themes["themes"]}
            for theme in ("monthly", "quarterly", "topic"):
                if theme not in theme_map:
                    self.blocked("legacy_" + theme, "服务端未开放此主题")
                    continue
                chosen_month, period = month, ""
                if theme == "quarterly":
                    quarters = [q for q in theme_map[theme].get("period_options", [])
                                if all(f"{q[:4]}-{m:02d}" in actual_months for m in range((int(q[-1]) - 1) * 3 + 1, int(q[-1]) * 3 + 1))]
                    if not quarters:
                        self.blocked("legacy_quarterly", "没有三个月资料完整的可用季度")
                        continue
                    period = "2026-Q2" if "2026-Q2" in quarters else quarters[-1]
                    chosen_month = period[:4] + "-" + f"{int(period[-1]) * 3:02d}"
                elif theme == "topic":
                    topics = theme_map[theme].get("period_options", [])
                    if not topics:
                        self.blocked("legacy_topic", "专题要素列表为空")
                        continue
                    period = "直接材料" if "直接材料" in topics else topics[0]
                generated = self.step("legacy_" + theme + "_generate_offline", lambda theme=theme, chosen_month=chosen_month, period=period: self.legacy(theme, chosen_month, product, period))
                if generated:
                    for fmt in ("docx", "pdf"):
                        self.step("legacy_" + theme + "_export_" + fmt, lambda theme=theme, fmt=fmt: self.export_legacy(theme, fmt))
        return self.finish()

    def finish(self):
        for who in ("alice", "bob"):
            if who in self.account_names:
                try:
                    self.request(who, "POST", "/api/auth/logout")
                except Exception as exc:
                    self.report["checks"].append({"name": "logout_" + who, "status": "failed", "error": self.clean(str(exc))[:500]})
        for client in self.clients.values():
            client.close()
        statuses = Counter(item["status"] for item in self.report["checks"])
        self.report["summary"] = {"passed": statuses["passed"], "failed": statuses["failed"], "blocked": statuses["blocked"],
                                  "artifact_count": len(self.report["artifacts"]),
                                  "http_passed": statuses["failed"] == 0 and statuses["blocked"] == 0}
        self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.flush()
        print(json.dumps({"event": "complete", "results": str(self.output / "results.json"), **self.report["summary"]}, ensure_ascii=False), flush=True)
        return 0 if self.report["summary"]["http_passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="明确指定待验收服务，例如 http://127.0.0.1:8765")
    parser.add_argument("--output-dir", required=True, type=Path, help="结果与导出文件父目录；每次新建独立子目录")
    parser.add_argument("--data-root", type=Path, help="可选：服务端数据根，只检查本次创建账号的costing_datasets目录")
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("base-url必须为HTTP(S)地址，不能包含账号密码、查询参数或片段")
    acceptance = Acceptance(args.base_url, args.output_dir, args.data_root)
    try:
        return acceptance.run()
    except Exception as exc:
        acceptance.report["checks"].append({"name": "acceptance_runner", "status": "failed", "error_type": type(exc).__name__, "error": acceptance.clean(str(exc))[:1500]})
        return acceptance.finish()


if __name__ == "__main__":
    raise SystemExit(main())
