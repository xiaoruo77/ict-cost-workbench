# -*- coding: utf-8 -*-
"""
app.py — **基于 llm-wiki 的智能分析面板**后端（FastAPI，只读）

设计原则
--------
1. **只读**。看板不修改任何知识库内容——它消费 wiki/state/reports。
   要改数据，走摄入流程（`ingest_agent.py`），不从这里点按钮。
2. **复用，不重造**。数据层直接 import 现成模块：
     数字/趋势 → `scripts/report_facts.py`（确定性取数，已带溯源坐标）
     数值快照 → `scripts/state.py`
     报告     → `reports/*.md`
   后端**自己不算任何数**——算数只有一处，就是 report_facts。
3. **派生视图不落地**。看板数据每次实时读文件，不建缓存的"第二真相"。

端点
----
    GET /api/health                      健康检查
    GET /api/products                    产品列表
    GET /api/overview?month=2026-05      三产品关键指标
    GET /api/trend?product=银黄口服液     逐月成本趋势
    GET /api/conflicts                   全库 Status 块（数据源自相矛盾处）
    GET /api/timeline                    摄入活动时间线（来自 log.md）
    GET /api/reports                     已生成的报告清单
    GET /api/report/{product}/{month}    单份报告 markdown
    GET /api/search?q=...                本地 BM25 / 当前核算快照来源检索

运行
----
    python server/app.py                 # 默认 127.0.0.1:8765
    python -m uvicorn app:app --reload   # 在 server/ 目录下开发
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
sys.path.insert(0, str(WIKI_ROOT / "scripts"))

import report_facts as RF  # noqa: E402
from errors import AgentError, ConfigError, DataMissing  # noqa: E402

app = FastAPI(title="基于 llm-wiki 的智能分析面板 API", version="1.0.0",
              description="只读。数据来自 llm-wiki 知识库（证据可溯）。")


# ---------------------------------------------------------------------------
# 统一错误响应
# ---------------------------------------------------------------------------
# ⚠️ 为什么必须有这一段（本库踩过的真实事故）：
#
#   数据缺失曾经用 `raise SystemExit(...)` 报。`SystemExit` 继承 **BaseException**，
#   而本文件所有兜底都是 `except Exception`（见 chat / search / decide / export
#   等处）——**一处都拦不住**。
#
#   后果不是"报错"而是**请求直接断连**：HTTP 层没有响应，浏览器只看到
#   connection reset，前端把"缺个 CSV"显示成"网络错误"，
#   排查时看不出是哪个请求、缺哪个文件。
#
#   ⇒ 可预期的失败（缺数据 / 缺配置）必须给出**结构化 JSON + 明确的下一步**。

@app.exception_handler(DataMissing)
async def _on_data_missing(_req: Request, exc: DataMissing) -> JSONResponse:
    # 503 而非 500：这是"暂时取不到数"，不是"服务写错了"。
    return JSONResponse(status_code=503, content=exc.as_dict())


@app.exception_handler(ConfigError)
async def _on_config_error(_req: Request, exc: ConfigError) -> JSONResponse:
    # 501 语义上不贴切，但这里要的是"能区分"：缺 Key 与缺数据是两回事，
    # 排查动作完全不同（配 .env vs 放数据），状态码分开更好定位。
    return JSONResponse(status_code=501, content=exc.as_dict())


@app.exception_handler(AgentError)
async def _on_agent_error(_req: Request, exc: AgentError) -> JSONResponse:
    return JSONResponse(status_code=500, content=exc.as_dict())



# Industry-neutral platform routes. Dataset roots are trusted server configuration;
# callers provide registered identifiers, never filesystem paths or Python modules.
def _cost_platform():
    from cost_platform import create_service
    return create_service(WIKI_ROOT)


def _costing_platform(user=None):
    from cost_platform import create_costing_service
    workspace = None
    if user is not None:
        import paths
        workspace = Path(paths.ensure_workspace(user["external_id"])).resolve()
        if workspace == Path(WIKI_ROOT).resolve():
            raise HTTPException(500, "个人工作区与共享项目目录不能相同")
        paths.recover_legacy_costing_datasets(user["external_id"], user["user_id"])
    return create_costing_service(WIKI_ROOT, user_workspace=workspace)


def _require_costing_user(request: Request):
    # Resolve _whoami at request time; its definition appears later in this module.
    return _whoami(request.headers.get("authorization", ""),
                   request.cookies.get("lw_sid", ""))


def _optional_user(authorization="", lw_sid=""):
    if lw_sid or authorization:
        return _whoami(authorization, lw_sid)
    return None


def _snapshot_response(analysis, user, metadata=None, cost_object=""):
    """Bind all downstream views to an owned, immutable calculation revision."""
    if user is None:
        return {**analysis, "scope": "shared_sample", "requires_login_to_save": True}
    import costing_outputs as outputs
    result = analysis["result"]
    meta = metadata or {}
    context = {
        "dataset_id": result["dataset_id"], "period": result["period"],
        "scenario": result["scenario"], "cost_object": cost_object,
        "owner_external_id": user["external_id"], "workspace_id": user["external_id"],
        "dataset_label": meta.get("label", result["dataset_id"]),
        "dataset_version": str(meta.get("version", "1")),
        "industry": meta.get("industry", result.get("industry_profile", {}).get("industry_code", "")),
        "plugin_version": str(meta.get("plugin_version", meta.get("version", "1"))),
        "configuration_sha256": meta.get("configuration_sha256", ""),
        "scope": "personal" if result["dataset_id"].startswith(("csv-", "declarative-")) else "shared_sample",
    }
    return outputs.create_snapshot(result, workspace_root=_ws(user["external_id"]),
                                   source_root=WIKI_ROOT, context=context)


def _owned_snapshot(analysis_id, user, expected_revision=""):
    import costing_outputs as outputs
    try:
        snapshot = outputs.load_snapshot(analysis_id, workspace_root=_ws(user["external_id"]))
        if snapshot.get("context", {}).get("owner_external_id") != user["external_id"]:
            raise FileNotFoundError("analysis snapshot not found")
        if expected_revision and not hmac.compare_digest(str(snapshot.get("revision", "")), expected_revision):
            raise HTTPException(409, "所请求的修订与该分析结果不一致")
        return snapshot
    except (FileNotFoundError, LookupError, ValueError):
        raise HTTPException(404, "该分析结果不存在或不属于当前账户") from None


_MODEL_LOCK = threading.Lock()
_REPORT_LOCK = threading.Lock()
_MODEL_ACTIVE = defaultdict(int)
_MODEL_REQUESTS = defaultdict(deque)
MODEL_MAX_PER_USER = 1
MODEL_MAX_TOTAL = 4
MODEL_RATE_PER_MINUTE = 12


def _acquire_model_slot(info):
    """Bound paid work before starting SSE/LLM, with release on every exit."""
    key = info["external_id"]
    now = time.monotonic()
    with _MODEL_LOCK:
        recent = _MODEL_REQUESTS[key]
        while recent and recent[0] < now - 60:
            recent.popleft()
        if (_MODEL_ACTIVE[key] >= MODEL_MAX_PER_USER or
                sum(_MODEL_ACTIVE.values()) >= MODEL_MAX_TOTAL):
            raise HTTPException(429, "已有分析任务正在运行，请等待结束后重试", headers={"Retry-After": "5"})
        if len(recent) >= MODEL_RATE_PER_MINUTE:
            raise HTTPException(429, "模型请求过于频繁，请稍后重试", headers={"Retry-After": "60"})
        _MODEL_ACTIVE[key] += 1
        recent.append(now)
    released = False
    def release():
        nonlocal released
        with _MODEL_LOCK:
            if not released:
                _MODEL_ACTIVE[key] -= 1
                released = True
    return release


@contextmanager
def _model_work(info):
    release = _acquire_model_slot(info)
    try:
        yield
    finally:
        release()


class _ModelStreamingResponse(StreamingResponse):
    """Release a reserved slot even if a disconnect precedes iteration.

    A generator's finally block never runs when it was never entered. Keep
    admission before HTTP headers so a full limit remains an actual 429, and
    bind cleanup to the ASGI response as well as the work generator.
    """
    def __init__(self, content, *, release_model, **kwargs):
        self._model_content = content
        self._release_model = release_model
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send):
        import anyio
        from starlette.concurrency import run_in_threadpool
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Closing suspended work also runs ingest's subprocess cleanup.
            # Shield cleanup from the cancellation that ended the response.
            with anyio.CancelScope(shield=True):
                try:
                    await run_in_threadpool(self._model_content.close)
                finally:
                    self._release_model()


def _platform_call(action):
    from cost_core.registry import UnknownDatasetError, UnknownPluginError
    try:
        return action()
    except (UnknownDatasetError, UnknownPluginError) as exc:
        raise HTTPException(404, str(exc))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/platform/plugins", summary="已注册行业插件")
def platform_plugins():
    from cost_core.registry import to_primitive
    return {"plugins": to_primitive(_cost_platform().registry.list())}


@app.get("/api/platform/datasets", summary="数据集元数据")
def platform_datasets():
    return _platform_call(lambda: {"datasets": list(_cost_platform().datasets())})


@app.get("/api/platform/analysis", summary="通用主干分析结果")
def platform_analysis(dataset_id: str, cost_object: str, period: str,
                      plugin_id: Optional[str] = None, organization: Optional[str] = None):
    from cost_core.registry import AnalysisRequest
    request = AnalysisRequest(dataset_id=dataset_id, plugin_id=plugin_id,
        cost_object=cost_object, period=period, organization=organization)
    return _platform_call(lambda: _cost_platform().analyze_payload(request))


@app.get("/api/platform/report-preview", summary="离线报告预览 零模型调用")
def platform_report_preview(dataset_id: str, cost_object: str, period: str,
                             plugin_id: Optional[str] = None, organization: Optional[str] = None):
    from cost_core.registry import AnalysisRequest
    request = AnalysisRequest(dataset_id=dataset_id, plugin_id=plugin_id,
        cost_object=cost_object, period=period, organization=organization)
    return _platform_call(lambda: _cost_platform().preview_report(request))


@app.get("/api/platform/industry-catalog", summary="GB/T 国民经济行业目录")
def platform_industry_catalog():
    from cost_core.industry_catalog import catalog_payload
    return catalog_payload()


@app.get("/api/platform/industry-contract/{code}", summary="行业成本申报模板")
def platform_industry_contract(code: str):
    from cost_core.declarative import contract
    try:
        return contract(code)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/platform/industry-analysis", summary="按行业成本模板核算")
async def platform_industry_analysis(request: Request, user=Depends(_require_costing_user)):
    import csv, io, hashlib
    from cost_core.declarative import compile_input, contract, configured_metrics
    from cost_core.costing import CostingEngine
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 10 * 1024 * 1024:
            raise HTTPException(413, "成本数据超过10MB限制")
        body.extend(chunk)
    try:
        if request.headers.get("content-type","").split(";",1)[0].strip().lower() == "text/csv":
            reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")), strict=True)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("CSV header missing or duplicated")
            rows = []
            for row in reader:
                if len(rows) == 100000 or None in row or any(v is None for v in row.values()):
                    raise ValueError("CSV has too many rows or excess columns")
                rows.append(row)
            payload = dict(version=request.query_params.get("version"),
                           industry_code=request.query_params.get("industry_code"),
                           period=request.query_params.get("period"),rows=rows)
            shared = request.query_params.get("shared_amount")
            if shared is not None:
                payload["shared_amount"] = shared
            shared_pools = request.query_params.get("shared_pools")
            if shared_pools is not None:
                payload["shared_pools"] = json.loads(shared_pools)
        else:
            payload = json.loads(body)
        compiled = compile_input(payload)
        result = CostingEngine().analyze(compiled)
        result["configured_metrics"] = configured_metrics(compiled, contract(payload["industry_code"]))
        profile_hash = hashlib.sha256(json.dumps(contract(payload["industry_code"]),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        result["industry_profile"] = {"industry_code": payload["industry_code"], "contract_version":payload["version"],
                                      "contract_sha256":profile_hash, "input_sha256":hashlib.sha256(body).hexdigest()}
    except (ValueError, ArithmeticError, csv.Error) as exc:
        raise HTTPException(422, str(exc)) from exc
    return _snapshot_response({"result": result}, user, {"label": "行业声明式成本 · " + payload["industry_code"],
                                             "version": payload["version"]})


@app.get("/api/platform/costing-industries", summary="通用成本分析行业与数据集")
def platform_costing_industries(authorization: str = Header(default=""),
                                lw_sid: str = Cookie(default="")):
    user = _optional_user(authorization, lw_sid)
    service = _costing_platform(user)
    from cost_core.costing import costing_to_primitive
    return {"plugins": costing_to_primitive(service.registry.list()),
            "datasets": list(service.datasets()),
            "diagnostics": getattr(service, "dataset_diagnostics", [])}


@app.get("/api/platform/costing-analysis", summary="通用成本台账与分摊分析")
def platform_costing_analysis(dataset_id: str, period: str, scenario: str = "actual",
                              cost_object: str = "",
                              authorization: str = Header(default=""),
                              lw_sid: str = Cookie(default="")):
    user = _optional_user(authorization, lw_sid)
    if dataset_id.startswith("csv-") and user is None:
        raise HTTPException(401, "个人成本数据集需要登录后查看")
    try:
        service = _costing_platform(user)
        analysis = service.analyze(dataset_id, period, scenario, cost_object)
        metadata = next((m for m in service.datasets() if m["dataset_id"] == dataset_id), {})
        return _snapshot_response(analysis, user, metadata, cost_object)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/platform/costing-import", summary="导入并注册通用成本CSV数据集")
async def platform_costing_import(file: UploadFile = File(...),
                                  schema_json: str = Form(...),
                                  label: str = Form(""),
                                  user: Dict[str, Any] = Depends(_require_costing_user)):
    """Import a mapped long-form CSV into the caller's private workspace.

    Uploads are size-limited, schema-validated and calculated before persistence.
    The original filename is never used as a path. Dataset IDs are server-generated.
    """
    import hashlib
    import shutil
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "CSV文件超过10MB限制")
    if len(schema_json.encode("utf-8")) > 256 * 1024:
        raise HTTPException(413, "字段映射超过256KB限制")
    try:
        text = raw.decode("utf-8-sig")
        schema = json.loads(schema_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "CSV必须为UTF-8编码，schema_json必须是合法JSON") from exc
    if not isinstance(schema, dict):
        raise HTTPException(422, "schema_json必须是JSON对象")
    if schema.get("plugin_id", "csv-generic") != "csv-generic":
        raise HTTPException(422, "CSV导入固定使用 csv-generic 适配器，不能指定其他 plugin_id")
    dataset_id = "csv-" + secrets.token_hex(8)
    schema = dict(schema)
    schema["dataset_id"] = dataset_id
    schema["plugin_id"] = "csv-generic"
    from cost_core.csv_adapter import CsvCostingAdapter
    from cost_core.costing import CostingEngine
    try:
        request = CsvCostingAdapter().from_text(text, schema)
        # Validate calculations and all declared allocations before storing anything.
        CostingEngine().analyze(request)
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(422, "成本数据校验失败：" + str(exc)) from exc

    import paths
    workspace = Path(paths.ensure_workspace(user["external_id"])).resolve()
    if workspace == Path(WIKI_ROOT).resolve():
        raise HTTPException(500, "个人工作区与共享项目目录不能相同")
    parent = workspace / "costing_datasets"
    parent.mkdir(parents=True, exist_ok=True)
    destination = (parent / dataset_id).resolve()
    if not destination.is_relative_to(parent.resolve()) or destination.exists():
        raise HTTPException(409, "数据集目录冲突")
    # A staging workspace exercises the same registry/manifest/reload path as
    # later requests before publishing the dataset directory.
    staging = parent / (".import-" + secrets.token_hex(8))
    staged_dataset = staging / "costing_datasets" / dataset_id
    committed = False
    try:
        staged_dataset.mkdir(parents=True, exist_ok=False)
        with (staged_dataset / "data.csv").open("xb") as stream:
            stream.write(raw)
        safe_label = str(label or schema.get("label") or "导入成本数据集").strip()[:100]
        manifest = {
            "dataset_id": dataset_id, "label": safe_label,
            "industry": str(schema.get("industry") or "custom")[:64],
            "version": "1", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "uploaded_by": user["external_id"],
            "data_sha256": hashlib.sha256(raw).hexdigest(), "schema": schema,
        }
        with (staged_dataset / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, separators=(",", ":"))
        from cost_platform import create_costing_service
        staged_service = create_costing_service(WIKI_ROOT, user_workspace=staging)
        metadata = next(item for item in staged_service.datasets() if item["dataset_id"] == dataset_id)
        analysis = staged_service.analyze(dataset_id, request.period, request.scenario)
        json.dumps({"dataset": metadata, "analysis": analysis}, allow_nan=False)
        staged_dataset.rename(destination)
        committed = True
        analysis = _snapshot_response(analysis, user, metadata)
        response = {"dataset": metadata, "analysis": analysis}
        json.dumps(response, allow_nan=False)
        return response
    except (ValueError, ArithmeticError) as exc:
        if committed and destination.exists():
            shutil.rmtree(destination)
        raise HTTPException(422, "导入未保存：" + str(exc)) from exc
    except Exception:
        if committed and destination.exists():
            shutil.rmtree(destination)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


@app.post("/api/platform/costing-bundle-import", summary="导入多份原始CSV与同版行业规则")
async def platform_costing_bundle_import(files: List[UploadFile] = File(...),
                                         schema_json: str = Form(...),
                                         label: str = Form(""),
                                         user: Dict[str, Any] = Depends(_require_costing_user)):
    """Atomically retain originals, compile their references and use one engine."""
    import csv
    import io
    import hashlib
    import shutil
    from cost_core.csv_adapter import CsvCostingAdapter
    from cost_core.costing import CostingEngine
    from industry_plugins.costing_plugins import configuration_sha256
    if len(schema_json.encode("utf-8")) > 256 * 1024:
        raise HTTPException(413, "字段映射超过256KB限制")
    try:
        schema = json.loads(schema_json)
        if not isinstance(schema, dict):
            raise ValueError("规则必须是JSON对象")
        if schema.get("plugin_id", "csv-generic") != "csv-generic":
            raise ValueError("多源导入固定使用csv-generic适配器")
        declared = schema.get("raw_composite", {}).get("inputs")
        if not isinstance(declared, dict):
            raise ValueError("多源规则必须声明命名来源")
        CsvCostingAdapter.validate_schema(schema)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "规则校验失败：" + str(exc)) from exc
    if not 1 <= len(files) <= 16:
        raise HTTPException(422, "须选择1至16份原始CSV，具体文件集合按规则校验")
    originals, texts, source_manifest = {}, {}, {}
    total_bytes = 0
    for upload in files:
        filename = upload.filename or ""
        match = re.fullmatch(r"([A-Za-z0-9_-]{1,40})\.csv", filename)
        if not match or match[1] in originals:
            raise HTTPException(422, "文件须按规则来源命名且不可重复：<来源标识>.csv；收到：" + filename[:100])
        name = match[1]
        raw = await upload.read(10 * 1024 * 1024 + 1)
        total_bytes += len(raw)
        if len(raw) > 10 * 1024 * 1024 or total_bytes > 32 * 1024 * 1024:
            raise HTTPException(413, "每份CSV上限10MB，多文件总上限32MB")
        try:
            text = raw.decode("utf-8")
            count = sum(1 for _ in csv.DictReader(io.StringIO(text), strict=True))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise HTTPException(422, "原始CSV编码或格式错误：" + name) from exc
        originals[name], texts[name] = raw, text
        source_manifest[name] = {"path": "sources/" + name + ".csv", "sha256": hashlib.sha256(raw).hexdigest(),
                                 "original_filename": filename, "record_count": count}
    dataset_id = "csv-" + secrets.token_hex(8)
    schema = {**schema, "dataset_id": dataset_id, "plugin_id": "csv-generic"}
    try:
        request = CsvCostingAdapter().from_files(texts, schema, schema_uri="upload://schema")
        CostingEngine().analyze(request)
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(422, "成本数据校验失败：" + str(exc)) from exc
    import paths
    workspace = Path(paths.ensure_workspace(user["external_id"])).resolve()
    if workspace == Path(WIKI_ROOT).resolve():
        raise HTTPException(500, "个人工作区与共享项目目录不能相同")
    parent = workspace / "costing_datasets"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / dataset_id
    if destination.exists():
        raise HTTPException(409, "数据集目录冲突")
    staging = parent / (".import-" + secrets.token_hex(8))
    staged_dataset = staging / "costing_datasets" / dataset_id
    committed = False
    try:
        (staged_dataset / "sources").mkdir(parents=True, exist_ok=False)
        for name, raw in originals.items():
            with (staged_dataset / "sources" / (name + ".csv")).open("xb") as stream:
                stream.write(raw)
        manifest = {
            "storage_format": "costing-bundle/1", "dataset_id": dataset_id,
            "label": str(label or schema.get("label") or "多源成本数据集").strip()[:100],
            "industry": schema.get("industry", "custom"), "version": "1",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "uploaded_by": user["external_id"], "source_files": source_manifest,
            "configuration_sha256": configuration_sha256(schema), "schema": schema,
        }
        with (staged_dataset / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        from cost_platform import create_costing_service
        service = create_costing_service(WIKI_ROOT, user_workspace=staging)
        metadata = next(item for item in service.datasets() if item["dataset_id"] == dataset_id)
        analysis = service.analyze(dataset_id, request.period, request.scenario)
        json.dumps({"dataset": metadata, "analysis": analysis}, allow_nan=False)
        staged_dataset.rename(destination)
        committed = True
        snapshot = _snapshot_response(analysis, user, metadata)
        return {"dataset": metadata, "analysis": snapshot, "source_files": source_manifest}
    except (ValueError, ArithmeticError) as exc:
        if committed and destination.exists():
            shutil.rmtree(destination)
        raise HTTPException(422, "多源导入未保存：" + str(exc)) from exc
    except Exception:
        if committed and destination.exists():
            shutil.rmtree(destination)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)



@app.get("/api/platform/costing-examples", summary="可复测行业合成示例")
def platform_costing_examples():
    from costing_workbench import list_examples
    try:
        value = list_examples()
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "行业示例配置暂不可用") from exc
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


@app.post("/api/platform/costing-examples/{example_id}/import", summary="将合成示例导入个人成本工作区")
async def platform_costing_example_import(example_id: str, user=Depends(_require_costing_user)):
    import io
    from costing_workbench import get_example
    try:
        metadata, schema, sources = get_example(example_id)
    except LookupError:
        raise HTTPException(404, "没有此行业示例") from None
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "此示例资料暂不可用") from exc
    uploads = [UploadFile(filename=name, file=io.BytesIO(data))
               for name, data in sources.items()]
    try:
        return await platform_costing_bundle_import(
            files=uploads, schema_json=json.dumps(schema, ensure_ascii=False),
            label=metadata["name"] + " · 合成示例 · " + metadata["period"], user=user)
    finally:
        for upload in uploads:
            await upload.close()


@app.get("/api/platform/costing-workspace", summary="当前用户成本数据与报告概览")
def platform_costing_workspace(user=Depends(_require_costing_user)):
    from costing_workbench import personal_reports, list_examples
    service = _costing_platform(user)
    datasets = [{**item, "ownership": "personal" if re.fullmatch(r"csv-[0-9a-f]{16}", item["dataset_id"])
                 else "shared_sample"} for item in service.datasets()]
    with _REPORT_LOCK:
        reports, report_diagnostics = personal_reports(
            _ws(user["external_id"]), user["external_id"], _report_record)
    diagnostics = list(getattr(service, "dataset_diagnostics", [])) + report_diagnostics
    try:
        examples_count = len(list_examples()["examples"])
    except (OSError, ValueError):
        examples_count = None
    from fastapi.encoders import jsonable_encoder
    return JSONResponse(jsonable_encoder({
        "datasets": datasets, "reports": reports, "diagnostics": diagnostics,
        "statistics": {
            "personal_datasets": sum(item["ownership"] == "personal" for item in datasets),
            "shared_datasets": sum(item["ownership"] == "shared_sample" for item in datasets),
            "saved_reports": len(reports),
            "unavailable_items": len(diagnostics),
        },
        "examples_count": examples_count,
    }), headers={"Cache-Control": "no-store"})


@app.get("/api/platform/costing-snapshots/{analysis_id}")
def costing_snapshot_get(analysis_id: str, revision: str = Query(""), user=Depends(_require_costing_user)):
    return _owned_snapshot(analysis_id, user, revision)


@app.get('/api/platform/costing-snapshots/{analysis_id}/object-summary')
def costing_object_only_summary(
    analysis_id: str,
    request: Request,
    revision: str = Query(..., pattern=r'^[0-9a-f]{64}$'),
    object_id: str = Query(..., min_length=1, max_length=128),
    user=Depends(_require_costing_user),
):
    from costing_object_summary import ObjectSummaryError, project_object_only
    headers = {'Cache-Control': 'no-store'}
    names = {'revision', 'object_id'}
    if set(request.query_params) != names or any(len(request.query_params.getlist(key)) != 1 for key in names):
        raise HTTPException(422, '只接受所选对象和精确报告版本', headers=headers)
    try:
        summary = project_object_only(
            analysis_id, _ws(user['external_id']),
            expected_revision=revision, expected_workspace_owner=user['external_id'],
            object_id=object_id,
        )
    except ObjectSummaryError:
        raise HTTPException(404, '没有可用的单对象摘要', headers=headers) from None
    return JSONResponse(summary, headers=headers)


@app.get('/api/platform/costing-month-comparison')
def costing_month_comparison(
    request: Request,
    previous_id: str = Query(..., pattern=r'^[0-9a-f]{32}$'),
    previous_revision: str = Query(..., pattern=r'^[0-9a-f]{64}$'),
    current_id: str = Query(..., pattern=r'^[0-9a-f]{32}$'),
    current_revision: str = Query(..., pattern=r'^[0-9a-f]{64}$'),
    user=Depends(_require_costing_user),
):
    # A caller cannot provide a workspace, book, or accounting-policy authority.
    from costing_monthly_guard import (
        MonthlyPreviewIncomparable, MonthlyPreviewUnavailable,
        MonthlyRevisionPin, guarded_monthly_preview,
    )
    headers = {'Cache-Control': 'no-store'}
    def unavailable():
        return HTTPException(404, '没有可用的同口径环比', headers=headers)

    names = {'previous_id', 'previous_revision', 'current_id', 'current_revision'}
    if set(request.query_params) != names or any(len(request.query_params.getlist(key)) != 1 for key in names):
        raise HTTPException(422, '只接受两期各自的结果标识和版本', headers=headers)
    authority = getattr(request.app.state, 'costing_monthly_binding_provider', None)
    if authority is None:
        raise unavailable()
    loaded = []
    for ident in (previous_id, current_id):
        try:
            loaded.append(_owned_snapshot(ident, user))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            loaded.append(None)
    if any(item is None for item in loaded):
        raise unavailable()
    try:
        result = guarded_monthly_preview(
            previous_snapshot=loaded[0], current_snapshot=loaded[1],
            previous_pin=MonthlyRevisionPin(previous_id, previous_revision),
            current_pin=MonthlyRevisionPin(current_id, current_revision),
            expected_owner=user['external_id'], authority=authority,
        )
    except MonthlyPreviewUnavailable:
        raise unavailable() from None
    except MonthlyPreviewIncomparable:
        raise HTTPException(422, '两份已授权报表无法作同口径环比', headers=headers) from None
    return JSONResponse(result, headers=headers)


@app.post("/api/platform/costing-snapshots/{analysis_id}/report")
def costing_snapshot_report(analysis_id: str, revision: str = Query(""), user=Depends(_require_costing_user)):
    snapshot = _owned_snapshot(analysis_id, user, revision)
    reports = _ws(user["external_id"]) / "reports"
    name = f"costing-{analysis_id}.md"
    metadata = {**snapshot["context"], "analysis_id": analysis_id,
                "revision": snapshot["revision"], "theme": "costing",
                "report_id": analysis_id, "rpa_supported": False,
                "cost_total": snapshot["result"]["totals"].get("total_cost"),
                "currency": snapshot["result"].get("currency"),
                "object_count": len(snapshot["result"].get("objects", [])),
                "ledger_reconciled": snapshot["result"].get("ledger_reconciled")}
    path = reports / name
    # An immutable snapshot may be saved repeatedly; its existing bytes must
    # remain equal, and no earlier report is overwritten.
    markdown = snapshot.get("report", {}).get("markdown", "")
    if not markdown:
        raise HTTPException(422, "该分析没有可保存的报告正文")
    with _REPORT_LOCK:
        if path.exists() and path.read_text(encoding="utf-8") != markdown:
            raise HTTPException(409, "已保存报告与该分析版本不一致，已保留原文件")
        temporary_md = reports / (".report-" + secrets.token_hex(8) + ".tmp")
        temporary_meta = temporary_md.with_name(temporary_md.name + ".meta")
        try:
            temporary_md.write_text(markdown, encoding="utf-8", newline="\n")
            temporary_meta.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary_meta, reports / (name + ".meta.json"))
            if not path.exists():
                os.replace(temporary_md, path)
        finally:
            temporary_md.unlink(missing_ok=True)
            temporary_meta.unlink(missing_ok=True)
    return {"ok": True, "file": name, "metadata": metadata,
            "analysis_id": analysis_id, "revision": snapshot["revision"]}


@app.get("/api/platform/costing-snapshots/{analysis_id}/export")
def costing_snapshot_export(analysis_id: str, format: str = Query("docx"),
                            revision: str = Query(""),
                            user=Depends(_require_costing_user)):
    if format not in {"docx", "pdf", "json", "md"}:
        raise HTTPException(422, "format只能是docx、pdf、json或md")
    import costing_outputs as outputs
    snapshot = _owned_snapshot(analysis_id, user, revision)
    output = outputs.export_snapshot(snapshot, format, _ws(user["external_id"]) / "exports")
    return FileResponse(str(output), filename=output.name,
                        media_type={"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                    "json": "application/json", "md": "text/markdown"}[format])


@app.get("/api/platform/costing-snapshots/{analysis_id}/evidence/{evidence_id}")
def costing_snapshot_evidence(analysis_id: str, evidence_id: str,
                              revision: str = Query(""),
                              user=Depends(_require_costing_user)):
    import costing_outputs as outputs
    snapshot = _owned_snapshot(analysis_id, user, revision)
    try:
        return outputs.resolve_evidence(snapshot, evidence_id, source_root=WIKI_ROOT,
                                        workspace_root=_ws(user["external_id"]))
    except (LookupError, FileNotFoundError, ValueError):
        raise HTTPException(404, "来源证据不存在或不属于当前分析结果") from None


@app.post("/api/platform/costing-snapshots/{analysis_id}/question")
async def costing_snapshot_question(analysis_id: str, request: Request,
                                    revision: str = Query(""),
                                    user=Depends(_require_costing_user)):
    import costing_outputs as outputs
    snapshot = _owned_snapshot(analysis_id, user, revision)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "问题必须是JSON对象") from None
    question = body.get("question") if isinstance(body, dict) else None
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise HTTPException(422, "question需要1—2000字的非空字符串")
    answer = outputs.answer_snapshot(snapshot, question.strip())
    return {**answer, "analysis_id": analysis_id, "revision": snapshot["revision"],
            "context": snapshot["context"]}


@app.get("/api/platform/costing-snapshots/{analysis_id}/charts")
def costing_snapshot_charts(analysis_id: str, revision: str = Query(""), user=Depends(_require_costing_user)):
    import costing_outputs as outputs
    snapshot = _owned_snapshot(analysis_id, user, revision)
    return {"analysis_id": analysis_id, "revision": snapshot["revision"],
            "context": snapshot["context"], "charts": outputs.chart_data(snapshot)}


STATIC = HERE / "static"
WIKI = WIKI_ROOT / "wiki"
REPORTS = WIKI_ROOT / "reports"


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/api/health", summary="健康检查与知识库规模")
def health() -> Dict[str, Any]:
    """看板与知识库的连通性——同时暴露知识库规模，便于排查"数据为空"。"""
    n_wiki = len([p for p in WIKI.rglob("*.md")
                  if p.name not in ("index.md", "log.md")])
    raw_dir = WIKI_ROOT / "raw"
    raw_files = [p for p in raw_dir.rglob("*")
                 if p.is_file() and not p.name.startswith(".")]

    # **原始资料数 ≠ 文件数**。
    # PDF/Word/Excel 会各生成一个同名 `.txt` 派生缓存（供检索用）。
    # 对用户说"基于 N 份原始资料"时**必须排除**它们，否则是虚报。
    # 判据：某 .txt 与一个同名二进制件（.pdf/.docx/.xlsx/.xls）并存 → 它是派生缓存。
    BINARY = {".pdf", ".docx", ".xlsx", ".xls", ".doc"}
    sources = [
        p for p in raw_files
        if not (p.suffix.lower() == ".txt"
                and any(p.with_suffix(e).exists() for e in BINARY))
    ]
    return {
        "ok": True,
        "articles": n_wiki,
        "sources": len(sources),          # 用户视角：多少份原始资料
        "raw_files": len(raw_files),
        "reports": len(list(REPORTS.glob("*.md"))) if REPORTS.exists() else 0,
    }


@app.get("/api/products", summary="产品列表")
def products() -> Dict[str, Any]:
    return {"products": RF.list_products()}


# ---------------------------------------------------------------------------
# 关键指标 / 趋势
# ---------------------------------------------------------------------------

# 看板关心的指标（report_facts 每条都带坐标）
#
# ⚠️ 上月值也要暴露：**瀑布图**要展示"上月 → 本月"的逐要素拆解，
#    只有本月值画不出瀑布。这些量 report_facts 里本来就有，只是原先没往外给。
OVERVIEW_KEYS = [
    "本月单位成本", "上月单位成本",
    "单位成本环比", "单位成本同比", "单位成本预算偏差",
    "本月材料成本", "上月材料成本",
    "本月人工成本", "上月人工成本",
    "本月制造成本", "上月制造成本",
    "材料占比", "人工占比", "制造费用占比",
    "材料贡献度", "人工贡献度", "制造费用贡献度",
]


def _overview_one(product: str, month: str) -> Dict[str, Any]:
    fs = RF.build(product, month)
    out: Dict[str, Any] = {"product": product, "month": month, "facts": {}, "coords": {}}
    for k in OVERVIEW_KEYS:
        f = fs.facts.get(k)
        if f is None:
            continue
        out["facts"][k] = f.value
        out["coords"][k] = f.coord
        out.setdefault("display", {})[k] = f.display
    out["missing"] = fs.missing
    return out


@app.get("/api/overview", summary="三产品关键指标")
def overview(month: str = Query("2026-05", description="分析月份 YYYY-MM")) -> Dict[str, Any]:
    return {"month": month,
            "items": [_overview_one(p, month) for p in RF.list_products()]}


@app.get("/api/trend", summary="逐月成本趋势")
def trend(product: str = Query(...), months: int = Query(6, ge=1, le=24),
          month: Optional[str] = Query(None)) -> Dict[str, Any]:
    """逐月单位成本趋势。直接来自 report_facts 的 monthly（已算好环比）。"""
    as_of = month or max(r["月份"] for r in RF._read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv"))
    fs = RF.build(product, as_of)
    rows = fs.monthly[-months:]
    return {"product": product,
            "series": [
                {"月份": r["月份"],
                 "单位成本": RF._num(r["单位成本"]),
                 "单位材料": RF._num(r["单位材料"]),
                 "单位人工": RF._num(r["单位人工"]),
                 "单位制造费用": RF._num(r["单位制造费用"]),
                 "产量": RF._num(r["产量"]),
                 "环比": r["环比"]}
                for r in rows]}


@app.get("/api/forecast", summary="成本趋势预测（加分项）")
def forecast_api(product: str = Query(...), horizon: int = Query(2, ge=1, le=6),
                 authorization: str = Header(default=""),
                 lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    下月/未来数月单位成本预测（Holt 线性趋势）。

    ⚠️ **预测值不是"事实"**：它没有 raw 坐标、`check_math` 复算不了它。
       所以本接口的返回里**显式带 `warnings` 与 `trustworthy`**，
       前端必须把它们显示出来——一个孤零零的"下月 11.17 元/盒"
       会被当成承诺读，而它不是。

    本端点只读，不需要登录（与其余看板数据一致）。
    """
    import forecast as FC
    fc = FC.forecast(product, horizon)
    return {
        "product": fc.product,
        "n_samples": fc.n_samples,
        "rmse": round(fc.rmse, 4),
        "alpha": fc.alpha, "beta": fc.beta,
        "trustworthy": fc.trustworthy,
        "history": [{"月份": m, "单位成本": v} for m, v in fc.history],
        "predictions": fc.predictions,
        "warnings": fc.warnings,
        # 让前端不必自己拼这句话——口径统一由服务端给
        "method": "Holt 线性趋势（非季节性模型）",
    }


# ---------------------------------------------------------------------------
# 冲突登记表 —— 解析全库 Status 块
# ---------------------------------------------------------------------------

_STATUS_RE = re.compile(r"^>\s*\*\*Status:\s*(Disputed|Outdated|Update)\*\*(.*)$")


def _parse_conflicts() -> List[Dict[str, Any]]:
    """
    从 wiki 词条里抽 `> **Status: Disputed**` 块。

    ⚠️ 只抽**块**，不抽行——一个 Status 块由连续的 `>` 行组成，
    必须整块取出来才有"信源A / 信源B / 工程判定"的完整信息。
    """
    out: List[Dict[str, Any]] = []
    for p in sorted(WIKI.rglob("*.md")):
        if p.name in ("index.md", "log.md"):
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
        i = 0
        while i < len(lines):
            m = _STATUS_RE.match(lines[i].strip())
            if not m:
                i += 1
                continue
            kind, headline = m.group(1), m.group(2).strip(" ——-—")
            body: List[str] = []
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith(">"):
                body.append(lines[j].lstrip()[1:].strip())
                j += 1
            body = [b for b in body if b]
            # 多数 Status 块的描述在**下一行**（`> - 信源A...`），标题行本身是空的。
            # 空标题回退到正文首行，否则看板会显示一片空白。
            #
            # ⚠️ 回退时要**跳过两类无信息量的行**：
            #    · 「信源X（…）」——经用户化转换后变成
            #      「原始说明文档 声明： 如上表 《原始说明文档》」，读不懂
            #    · 「工程判定/处理方式：…」——是**结论**不是**标题**，
            #      拿来当标题会变成「处理方式: 仅人工一条吻合…」
            #    两类都跳过还找不到，就回退到**所在章节名**（比正文首行更像标题）。
            if not headline and body:
                cand = next(
                    (b for b in body
                     if not re.match(r"^[-–—\s]*信源", b)
                     and not re.match(r"^[-–—\s]*(工程判定|处理方式|判定)", b)),
                    None)
                if cand is None:
                    cand = ""          # 交给下面的章节名兜底
                headline = re.sub(r"^[-–—\s]+", "", cand)[:120]
            # 定位所在章节
            sec = ""
            for k in range(i, -1, -1):
                if lines[k].startswith("#"):
                    sec = lines[k].lstrip("# ").strip()
                    break
            # 章节名兜底：这类块**本来就没有标题行**（整块都是信源+判定），
            # 硬拿正文行当标题只会得到一句读不通的话。用「某节 · 数值待核」更诚实。
            if not headline:
                headline = f"{sec}：数字待核对" if sec else "数字待核对"
            out.append({
                "article": p.relative_to(WIKI_ROOT).as_posix(),
                "section": sec,
                "kind": kind,
                "headline": headline,
                "body": body,
                "line": i + 1,
            })
            i = j
    return out


# ---- 去重与分级 -------------------------------------------------------------
#
# 为什么在**服务端**做，而不去改 wiki：
#   同一条发现分散在多个章节里各登记一次，在**它所在章节的上下文中是有意义的**
#   （读者在那一节就需要知道"这里的数字要小心"）。所以知识层不动。
#   而看板要的是"一共有几件事"，属于**呈现口径**——在出口处统一收敛。
#
# 实测的重复有两类：
#   ① 同文回指：章节里明确写「（见 2.2）」，是同一件事的另一处登记
#   ② 跨文重复：同一条发现在两篇文章各记一次
#
# ⚠️ **刻意用显式规则表，不用模糊相似度**：
#   宁可漏并（少合一条，用户多看一条），**不可错并**——
#   把两个不同的发现合成一条 = 凭空丢信息，且看不出来。

# 回指标记：标题里带「（见 X.Y）」说明是别处的重复登记
_BACKREF_RE = re.compile(r"[（(]见\s*\d+(?:\.\d+)*\s*[)）]")

# 主题归并：正则 → 规范主题名。命中同一主题名的合并为一条。
# 顺序有意义：先匹配到的先用（把更具体的放前面）。
_SUBJECT_RULES: List[tuple] = [
    (r"金银花.{0,10}12%|12%.{0,10}金银花",       "金银花涨幅 12% 不可复现"),
    (r"板蓝根.{0,10}10%|10%.{0,10}板蓝根",       "板蓝根涨幅 10% 只在单耗口径成立"),
    (r"处方量.{0,12}(单粒|单袋)含量|(单粒|单袋)含量.{0,12}处方量",
                                                 "处方量与单含量的量级不符"),
    (r"(占总材料成本比|材料成本占比).{0,6}表|配方.{0,8}成本占比表",
                                                 "配方成本占比表不可用"),
    (r"配方理论材料成本|理论材料成本",              "配方理论成本与实际的倍率口径"),
    # ⚠️ 不能要求出现「不符/差」：实测同一条发现有两种写法——
    #    「工艺人工定额与成本明细不符」与「工艺定额 vs 实际直接人工」，
    #    后者没有"不符"二字。要求关键词会**漏并**，两条并列显示。
    (r"(工艺|人工).{0,10}定额|定额.{0,8}(与|vs).{0,8}(实际|明细|成本)",
                                                 "工艺人工定额与成本明细不符"),
    (r"加班.{0,14}(工时|数据)|工时数据.{0,10}加班",  "加班因素缺乏工时数据支持"),
    (r"收率.{0,10}(波动|分离)|无法与价格效应分离",   "收率波动无法与价格效应分离"),
    # 同一条发现的两种写法：一处叫「对标差异率区间」，另一处正文说「超出声明区间」
    (r"对标差异率.{0,10}区间|差异率.{0,6}区间|声明(的)?区间|超出.{0,4}区间",
                                                 "对标差异率区间与实测不符"),
    (r"本厂水平.{0,10}(无数据|无法核验)|表中.{0,6}本厂水平",
                                                 "「本厂水平」无数据支撑"),
    (r"逐章条文区间|条文区间.{0,10}不符",           "GMP 章节条文区间与全文不符"),
    (r"请求路径|示例.{0,10}路径",                  "接口文档示例路径与实现不符"),
    (r"overdue",                                  "mock 未实现 overdue 状态"),
    (r"资产编号.{0,10}示例|编号示例",              "模板资产编号示例与实际不符"),
    (r"公式与表格|声明的公式.{0,10}不符",           "声明公式与表格数值不符"),
    (r"理由是错的|理由.{0,4}错",                   "原给出的理由已订正"),
]

# 影响分级：决定置顶还是折叠。
#   A = 影响**数字可信度**（用户据此判断结论能不能信）→ 置顶展开
#   B = **资料自身前后不一致**（不影响分析结论，供了解）→ 折叠
_SEVERITY_RULES: List[tuple] = [
    (r"涨幅|上涨|不可复现|倍率|口径|无数据|不可用", "A"),
]


def _subject_of(headline: str, body: List[str]) -> Optional[str]:
    """把一条冲突归到一个规范主题；归不上返回 None（**不合并**）。"""
    hay = headline + " " + " ".join(body[:4])
    for pat, name in _SUBJECT_RULES:
        if re.search(pat, hay):
            return name
    return None


def _severity_of(headline: str, body: List[str], kind: str) -> str:
    """A = 影响结论可信度；B = 资料内部不一致。"""
    if kind == "Update":
        return "A"          # 已订正的数值直接关系到结论，归 A
    hay = headline + " " + " ".join(body[:4])
    for pat, sev in _SEVERITY_RULES:
        if re.search(pat, hay):
            return sev
    return "B"


def _dedupe_and_grade(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    合并重复登记 + 标注影响级别。

    合并策略：同主题只保留**信息最全**的那条（body 最长）作为主条，
    其余合并进来——但**不丢信息**：

      · `merged_count` 记下合并了几条（**数条目，不数文章**——
        同一条可能在同文里登记两次，数文章会漏报）
      · 被合并条目的标题追加到主条正文，写清「另处登记」
        （否则板蓝根 1.8 倍这类细节会凭空消失，
         而"少显示"和"丢信息"是两回事）
    """
    groups: Dict[str, Dict[str, Any]] = {}
    passthrough: List[Dict[str, Any]] = []

    for it in items:
        subj = _subject_of(it.get("headline", ""), it.get("body", []))
        if subj is None:
            passthrough.append(it)
            continue
        cur = groups.get(subj)
        if cur is None:
            it["_subject"] = subj
            it["_merged_titles"] = []
            groups[subj] = it
            continue
        # 已有一组：body 更长的那条当主条（信息更全）
        if len(it.get("body", [])) > len(cur.get("body", [])):
            it["_subject"] = subj
            it["_merged_titles"] = cur["_merged_titles"] + [cur.get("headline", "")]
            groups[subj] = it
        else:
            cur["_merged_titles"].append(it.get("headline", ""))

    merged: List[Dict[str, Any]] = []
    for it in list(groups.values()) + passthrough:
        it["severity"] = _severity_of(it.get("headline", ""), it.get("body", []),
                                      it.get("kind", "Disputed"))
        titles = [t for t in it.get("_merged_titles", []) if t]
        it["merged_count"] = len(titles)
        # ⚠️ **只记标题，不搬被合并条目的正文**。
        #    曾经把它们的 body 也附上来"防止丢信息"，结果**串了主题**：
        #    处方量那条把「配方成本占比表不可用」「配方理论成本 2.20 vs 12.18」
        #    的正文也吸了过来——那是**另外两条独立发现**，不是同一件事。
        #    而标题本身已经带足了细节（"…差 1.8 倍"），搬正文纯属多余。
        if titles:
            it["body"] = list(it.get("body", [])) + [
                "另处登记：" + "；".join(titles[:5])
                + ("…" if len(titles) > 5 else "")]
        for k in ("_subject", "_merged_titles"):
            it.pop(k, None)
        merged.append(it)

    # A 类在前（影响可信度的先看到），同级按原文顺序稳定排序
    merged.sort(key=lambda x: (0 if x["severity"] == "A" else 1))
    return merged


@app.get("/api/conflicts", summary="数据口径说明（已知冲突）")
def conflicts() -> Dict[str, Any]:
    raw = [_polish(i) for i in _parse_conflicts()]
    items = _dedupe_and_grade(raw)
    by: Dict[str, int] = {"Disputed": 0, "Outdated": 0, "Update": 0}
    sev: Dict[str, int] = {"A": 0, "B": 0}
    for it in items:
        by[it["kind"]] = by.get(it["kind"], 0) + 1
        sev[it["severity"]] = sev.get(it["severity"], 0) + 1
    return {"count": len(items), "raw_count": len(raw),
            "by_kind": by, "by_severity": sev, "items": items}


# ---- 用户化转换 -------------------------------------------------------------
# 后端直接抽的是 wiki 原文，里面混着面向内部写作的措辞：
#   `信源A（README 声明）`、`[README.md:3.2 ...]`、`Status: Disputed`……
# 这些对最终使用者没有意义。**在数据出口处统一转一遍**，
# 比在前端到处补正则更可靠（前端只负责渲染）。
_SOURCE_WORDS = {
    "readme": "原始说明文档", "问题检查报告": "数据质量检查记录",
    "成本汇总": "成本汇总表", "原材料消耗明细": "原材料消耗明细表",
    "制造费用明细": "制造费用明细表", "人工工时明细": "人工工时明细表",
    "预算数据": "预算数据表", "药材市场价格行情": "药材市场行情表",
    "行业成本基准数据": "行业基准数据", "车间设备清单": "设备清单",
    "产品配方文档": "产品配方文档", "生产工艺文档": "生产工艺文档",
    "GMP法规核心摘要": "GMP 法规摘要", "药品生产质量管理规范": "GMP 全文",
    "月度成本分析报告模板": "报告模板", "模拟RPA接口文档": "工单接口文档",
    "药材行情月报": "行情月报",
    # 中药二厂后匹配，否则会被"成本汇总"先抓走、丢掉厂别
    "中药一厂": "一厂", "中药二厂": "二厂",
}

# 写作时用的内部标记，出现在正文里时按语义清掉
_STRIP_PHRASES = (
    "违反 SKILL.md 铁律 2.1（截断禁止）", "违反 SKILL.md 铁律 2.1", "SKILL.md 铁律 2.1",
    "（报告事实引擎）", "报告事实引擎", "scripts/report_facts.py", "report_facts.py",
    "scripts/check_math.py", "check_math.py", "scripts/", "SKILL.md",
)


_CITE = re.compile(r"\[([^\[\]]+?)\]")
_FILE_TOKEN = re.compile(r"\.(?:csv|md|pdf|txt|docx|xlsx|xls|py|json)\b", re.I)


def _friendly_file(fname: str) -> str:
    """
    文件名 → 使用者能懂的资料名。

    ⚠️ **必须带上年份**：`中药一厂_成本汇总_2025年…` 与 `…2026年…` 是两份不同的资料，
    都映射成「成本汇总表」会变成「《成本汇总表》+《成本汇总表》」，
    读者无法分辨对比的两边各是什么。
    """
    f = fname.strip()
    year = re.search(r"(20\d{2})", f)
    # 厂别先取出来——`中药二厂` 里也含"成本汇总"之后的匹配会被吞掉，
    # 导致两厂的同名表映射成一模一样的名字，读者分不清对比的两边。
    plant = "二厂" if "中药二厂" in f else ("一厂" if "中药一厂" in f else "")
    base = None
    for k, v in _SOURCE_WORDS.items():
        if k in ("中药一厂", "中药二厂"):
            continue                      # 厂别单独处理
        if k.lower() in f.lower():
            base = v
            break
    if base is None:
        base = re.sub(r"\.(csv|md|pdf|txt|docx|xlsx|xls|py|json)$", "", f, flags=re.I)
    base = f"{plant}{base}" if plant and plant not in base else base
    if year and "年" not in base:
        base = f"{year.group(1)} 年{base}"
    return base


def _userize(s: str) -> str:
    """把一句面向内部的措辞，转成使用者看得懂的话。"""
    t = str(s)
    # `[文件.csv:列名:筛选条件]` → 资料名（保留括号形态，读起来像"依据…"）
    def _cite(m: re.Match) -> str:
        inner = m.group(1)
        if not _FILE_TOKEN.search(inner):
            return m.group(0)          # 不是引用坐标，别动
        return f"《{_friendly_file(inner.split(':')[0])}》"
    # markdown 链接：`[文字](../costs/xx.md)` → 文字（保留可读部分，丢掉路径）
    t = re.sub(r"\[([^\[\]]+?)\]\([^)]*\)", r"\1", t)
    t = _CITE.sub(_cite, t)
    # 写作时的内部标记：清掉，但留下括号里真正有信息量的部分
    for ph in _STRIP_PHRASES:
        t = t.replace(ph, "")
    t = re.sub(r"S?KILL\.md\s*铁律\s*[\d.]+", "数值精度要求", t)
    t = re.sub(r"\b铁律\s*[\d.]+", "数值精度要求", t)
    # 「信源A（X）」→「X」；「信源B（本库的数据）」→「本系统数据」
    t = re.sub(r"信源\s*[A-Za-z]\s*[（(]([^）)]+)[）)]\s*[:：]?", r"\1：", t)
    t = t.replace("**", "").replace("`", "")
    t = re.sub(r"Status:\s*(Disputed|Outdated|Update)\s*(——|--|—)?\s*", "", t)
    t = t.replace("raw/", "").replace("raw 源数据自身", "原始资料本身").replace("raw", "原始资料")
    # 裸词也得换掉——它们没有被 `[…]` 包着，前面的 _cite 抓不到
    t = re.sub(r"\bREADME(\.md)?\b", "原始说明文档", t)
    t = t.replace("本库", "本系统").replace("本词条", "本项").replace("本 agent", "")
    t = t.replace("工程判定", "处理方式")
    t = re.sub(r"\s{2,}", " ", t)
    # 中英混排遗留的空格：`原始说明文档 的` → `原始说明文档的`。
    # ⚠️ 只压**汉字之间**的空格——ASCII 两边要保留，否则
    #    `工艺定额 vs 实际直接人工` 会变成 `定额vs实际`（读不断句）。
    t = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", t)
    # 去掉重复引用的同一份资料：`《A》、《A》` → `《A》`
    t = re.sub(r"(《[^》]+》)(?:\s*[、,]\s*\1)+", r"\1", t)
    return t.strip(" -—:：")


def _plain_section(s: str) -> str:
    """章节名去掉编号与内部用语。"""
    t = str(s).strip()
    # ⚠️ 编号可能带小数：`3.2 与 README …` —— 只削一层 `3.` 会留下 `2 与 …`
    t = re.sub(r"^\d+(?:\.\d+)*\s*[\.、]?\s*", "", t)
    t = re.sub(r"\bREADME(\.md)?\b", "原始说明文档", t)
    for bad in ("核验中发现的不可自洽之处", "核验中发现的问题", "不可自洽之处", "核验"):
        t = t.replace(bad, "数据核对说明")
    return t.strip() or "数据核对说明"


def _polish(it: Dict[str, Any]) -> Dict[str, Any]:
    o = dict(it)
    o["headline"] = _userize(it.get("headline", ""))
    o["section"] = _plain_section(it.get("section", ""))
    o["body"] = [_userize(b) for b in it.get("body", [])]
    return o


# ---------------------------------------------------------------------------
# 摄入活动时间线 —— 解析 log.md
# ---------------------------------------------------------------------------

_LOG_HDR = re.compile(r"^##\s*\[(\d{4}-\d{2}-\d{2})\]\s*([^|]+?)\s*\|\s*(.+?)\s*$")


def _parse_timeline() -> List[Dict[str, Any]]:
    p = WIKI / "log.md"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
    entries: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in lines:
        m = _LOG_HDR.match(ln.strip())
        if m:
            if cur:
                entries.append(cur)
            cur = {"date": m.group(1), "kind": m.group(2).strip(),
                   "title": m.group(3).strip(), "body": []}
            continue
        if cur is not None:
            cur["body"].append(ln)
    if cur:
        entries.append(cur)

    for e in entries:
        text = "\n".join(e["body"])
        e["disposition"] = ""
        dm = re.search(r"Disposition:\s*(.+)", text)
        if dm:
            # 同样清掉 markdown 记号（实测 `Update; **Outdated**` 会原样透出）
            e["disposition"] = dm.group(1).replace("**", "").replace("`", "").strip()
        e["files"] = sorted(set(re.findall(r"raw/[\w/一-鿿.\-（）]+", text)))[:6]

        # 摘要必须是**干净正文**——不能把 markdown 记号透给前端。
        # 实测踩过：直接 join 原文，前端显示成
        # `**这是本库第一次...** 验收「...」时（`finalize.py` 无条件...）`
        # 满屏 `**` 和反引号，还混进了代码围栏里的内容。
        # 元数据行不是正文，摘要里不该出现（否则每条 ingest 的摘要都是
        # "Disposition: New; Disputed Raw: raw/...csv; ..." —— 全是噪音）
        META = ("disposition:", "raw:", "updated:", "updated：", "产出:", "产出：")

        clean: List[str] = []
        in_fence = False
        for ln in e["body"]:
            s = ln.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if not s or s.startswith((">", "#", "|", "---")):
                continue
            s = re.sub(r"^[-*]\s+", "", s)              # 去列表符号
            s = re.sub(r"^\[[ x]\]\s*", "", s)          # 去复选框
            if s.lower().startswith(META):
                continue
            # 兜底：清掉任何残留的 markdown 记号字符。
            # 前面按对剥 `**x**` / `` `x` ``，但截断、嵌套、有意转义
            # （如 `\r`）都可能留下孤立记号——直接抹掉所有反引号与星号最稳。
            s = s.replace("`", "").replace("**", "")
            s = re.sub(r"^\s*[-*]\s*", "", s)
            s = s.strip()
            if s:
                clean.append(s)
        e["excerpt"] = " ".join(clean)[:190]
        e.pop("body", None)
    return entries


@app.get("/api/timeline", summary="数据更新记录")
def timeline() -> Dict[str, Any]:
    """
    数据更新记录。

    ⚠️ 只保留**与数据有关的**事件。日志里还有大量纯工程活动
    （部署、测试、后端搭建、依赖修复），那是开发记录，不是用户关心的
    "数据发生了什么"。把它们混进来会让这一页变成开发者变更日志。
    """
    # 面向用户的事件类型：纳入资料 / 校核 / 修正 / 规则调整
    USER_FACING = {"ingest", "lint", "fix", "skill"}
    items = [i for i in _parse_timeline() if i.get("kind") in USER_FACING]
    items = [_polish_timeline(i) for i in items]
    return {"count": len(items), "items": items}


# 工程活动里常见的词——出现即说明这条是开发记录而非数据记录
_DEV_NOISE = re.compile(
    r"(Dockerfile|docker|npm|node_modules|__pycache__|\.pyc|py_compile|"
    r"requirements\.txt|\.env|\.gitignore|\.dockerignore|PATH|"
    r"def |import |py\b|\.js\b|\.css\b|API 端点|FastAPI|React|"
    r"端口|进程|镜像|容器|卷|部署|构建|编译|重构|"
    r"[a-z_]+\.py\b|scripts/|"
    # 内部技术事故（用户不关心换行符、编码、依赖这类问题）
    r"换行符|污染|编码|依赖|超时|并发|性能|缓存|死循环|"
    r"根因|三层防御)", re.I)

# 摘要要不要展示的**严格判据**：
#   日志正文本质是开发记录，里面混着路径、英文标识符、命令片段。
#   与其写一堆正则把它们逐类擦掉（永远擦不干净，且会擦出病句），
#   不如**反向判断**——清洗后若仍含任何"像代码的东西"，就整条不显示。
#   宁可少给一句话，也不要给用户一段天书。
_LOOKS_LIKE_CODE = re.compile(
    r"([\w一-鿿\-]+/[\w一-鿿\-./]+"   # 路径 market/xxx.md
    r"|\b[A-Za-z_][A-Za-z0-9_]*\.(md|py|csv|json|txt|js|css|sql|yaml)\b"
    r"|\bStatus:\s*\w+"                                 # Status: Disputed
    r"|\b[A-Z][A-Za-z]{2,}(?=[\s,，。；])"              # 孤立英文词 Disputed/Update
    r"|\bpython\s|\bpip\s|\bgit\s|\bdocker\s"           # 命令
    r")")


def _safe_excerpt(s: str) -> str:
    """只有确认"不像代码"的摘要才放行，否则返回空。"""
    t = _dejargon(_userize(str(s or "")))
    if _DEV_NOISE.search(t) or _LOOKS_LIKE_CODE.search(t):
        return ""
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t if len(t) >= 12 else ""


def _polish_timeline(it: Dict[str, Any]) -> Dict[str, Any]:
    o = dict(it)
    o["title"] = _userize(_dejargon(clean_timeline_title(it.get("title", ""))))
    o["excerpt"] = _safe_excerpt(it.get("excerpt", ""))
    o["disposition"] = _dejargon(_userize(it.get("disposition", "")))
    # 处置结论里也只留"新增/修订/存疑"这类词，去掉括号里的技术细节
    d = o["disposition"]
    d = re.sub(r"[（(][^）)]*(\.md|\.py|\.csv|/[^）)]*)[）)]", "", d)
    o["disposition"] = re.sub(r"\s{2,}", " ", d).strip(" ;；,，")
    return o


def clean_timeline_title(t: str) -> str:
    """日志标题 → 用户标题。"""
    s = str(t)
    s = re.sub(r"（骨架 [A-G]'?）", "", s)
    s = re.sub(r"^(no material|未纳入)[：:]\s*", "未带来新信息：", s, flags=re.I)
    s = re.sub(r"\braw/[\w/一-龥.\-（）]+", "某份资料", s)
    # 测试题号（Q1/Q3/Q5）是内部验证用的编号 → 说成"一次提问"
    s = re.sub(r"^Q\d+\s*", "一次提问", s)
    # 工程向的标题直接换成中性说法
    if _DEV_NOISE.search(s):
        return "系统能力更新"
    return s.strip()


# 日志正文里频繁出现的规制用语 → 用户语言
_DISPOSITION_RE = re.compile(r"Disposition:\s*(.+)")
_JARGON = [
    (r"\breferences/[\w一-龥.\-]+\.md\b", "规则文档"),
    (r"\bscripts/[\w.\-]+\.py\b", "分析工具"),
    (r"\bSKILL\.md\b", "规则文档"),
    (r"\bingest_raw\.py\b", "资料收录工具"),
    (r"\braw/\S+", "原始资料"),
    (r"\bDisputed\b", "存在口径存疑"),
    (r"\bNo material\b", "未带来新信息"),
    (r"\bOutdated\b", "已过期"),
    (r"\bUpdate\b", "修订"),
    (r"\bNew\b", "新增"),
    (r"约束层", "规则"),
    (r"词条骨架", "文章模板"),
    (r"词条", "分析条目"),
    (r"级联", "关联更新"),
    (r"编译", "整理"),
    (r"入库", "纳入"),
]


def _dejargon(s: str) -> str:
    t = str(s)
    for pat, rep in _JARGON:
        t = re.sub(pat, rep, t)
    return t


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _report_record(path: Path, scope: str = "shared_sample") -> Dict[str, Any]:
    """Report identity is explicit metadata; legacy monthly names are migrated read-only."""
    sidecar = path.with_name(path.name + ".meta.json")
    metadata = {}
    if sidecar.is_file() and not sidecar.is_symlink():
        try:
            value = json.loads(sidecar.read_text(encoding="utf-8"))
            metadata = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            metadata = {}
    if not metadata:
        match = re.fullmatch(r"(?P<product>[^/\\]+)_(?P<period>20\d{2}-(?:0[1-9]|1[0-2]))\.md", path.name)
        if match:
            metadata = {"product": match.group("product"), "period": match.group("period"),
                        "theme": "monthly", "dataset_id": "pharma-sample-v1", "scenario": "actual",
                        "metadata_source": "legacy_monthly_name", "rpa_supported": True}
        else:
            metadata = {"theme": "unknown", "rpa_supported": False,
                        "metadata_status": "missing", "metadata_source": "unavailable"}
    metadata.setdefault("scope", scope)
    metadata.setdefault("report_id", path.stem)
    metadata.setdefault("rpa_supported", metadata.get("theme") == "monthly")
    return {"name": path.name, "file": path.name, "stem": path.stem,
            "size": path.stat().st_size, "metadata": metadata,
            "product": metadata.get("product", ""), "period": metadata.get("period", ""),
            "month": metadata.get("period", "") if metadata.get("theme") == "monthly" else "",
            "theme": metadata.get("theme", "unknown"), "rpa_supported": metadata["rpa_supported"],
            "analysis_id": metadata.get("analysis_id", ""), "scope": scope}


def _find_report(name: str, info) -> tuple[Path, Dict[str, Any]]:
    if (not name or Path(name).name != name or "/" in name or "\\" in name
            or not name.endswith(".md")):
        raise HTTPException(422, "需要报告列表中的完整Markdown文件名")
    for root, scope in ((_ws(info["external_id"]) / "reports", "personal"), (REPORTS, "shared_sample")):
        path = root / name
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
            record = _report_record(path, scope)
            if record["metadata"].get("owner_external_id", info["external_id"]) != info["external_id"]:
                continue
            return path, record
    raise HTTPException(404, "报告不存在或不属于当前账户")

@app.get("/api/reports", summary="已生成的报告清单")
def reports_list() -> Dict[str, Any]:
    if not REPORTS.exists():
        return {"count": 0, "items": []}
    items = []
    for p in sorted(REPORTS.glob("*.md")):
        if not p.is_symlink():
            items.append(_report_record(p))
    return {"count": len(items), "items": items}


@app.get("/api/report/{product}/{month}", summary="读取单份报告")
def report_get(product: str, month: str) -> Dict[str, Any]:
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", month) or re.search(r"[/\\]", product):
        raise HTTPException(422, "产品与月份格式无效")
    p = REPORTS / f"{product}_{month}.md"
    if not p.exists():
        raise HTTPException(404, f"报告不存在: {p.name}")
    return {**_report_record(p), "file": p.name, "product": product, "month": month,
            "markdown": p.read_text(encoding="utf-8")}


# ---------------------------------------------------------------------------
# 检索 —— 当前工作区本地 BM25，或当前核算快照来源
# ---------------------------------------------------------------------------

@app.get("/api/search", summary="授权资料的本地BM25检索 / 当前分析来源检索")
def search(q: str = Query(..., min_length=1), top_k: int = Query(6, ge=1, le=20),
           analysis_id: str = Query(""),
           authorization: str = Header(default=""),
           lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    本地 BM25 检索；有 analysis_id 时检索该用户所选核算快照来源。

    ⚠️ 登录用户的检索范围是「共享基线 + **他自己的**个人资料」；
       未登录只搜共享基线（未登录时没有"他"可言）。
       个人工作区**只取当前会话用户的那一个**——传错就是跨用户泄漏。
    """
    import paths
    if analysis_id:
        user = _whoami(authorization, lw_sid)
        snapshot = _owned_snapshot(analysis_id, user)
        evidence = snapshot.get("evidence", [])
        terms = [word for word in q.casefold().split() if word]
        hits = [item for item in evidence
                if all(term in json.dumps(item, ensure_ascii=False).casefold() for term in terms)][:top_k]
        return {"query": q, "analysis_id": analysis_id, "revision": snapshot["revision"],
                "context": snapshot["context"], "scope": "analysis_snapshot",
                "count": len(hits), "hits": hits}
    info = _optional_user(authorization, lw_sid)
    ov = ({"shared_root": WIKI_ROOT, "personal_root": paths.ensure_workspace(info["external_id"])}
          if info else {})

    try:
        import retrieval
        # 先确保索引覆盖当前语料（含个人层）。hybrid_search 内部也会查覆盖率，
        # 这里显式建一次，让"个人块缺向量"在入口就被修掉。
        idx = None
        try:
            idx = retrieval.build_index(verbose=False, root=WIKI_ROOT, **ov)
        except Exception:  # noqa: BLE001
            idx = None               # 离线/无 key 时退回只用 BM25
        hits = retrieval.hybrid_search(q, top_k=top_k, root=WIKI_ROOT,
                                       index=idx, allow_online=False, **ov)
        return {"query": q, "count": len(hits),
                "scope": "shared+personal" if info else "shared",
                "hits": [{"article": getattr(h.chunk, "article", ""),
                          "section": getattr(h.chunk, "section", ""),
                          "source": getattr(h.chunk, "source", "shared"),
                          "text": getattr(h.chunk, "text", "")[:400],
                          "score": getattr(h, "rrf", None)} for h in hits]}
    except Exception as e:  # noqa: BLE001
        return {"query": q, "count": 0, "hits": [], "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------

#
# 图谱是**派生视图**（`graph/graph.json`，由 graph_build.py 从 wiki 抽取）。
# 这里只读、不重建——重建归 finalize.py。读不到就如实报，别假装有图。
# ---------------------------------------------------------------------------

_GRAPH_TYPE_ZH = {
    "product": "产品", "material": "原料", "process": "工序",
    "equipment": "设备", "incident": "故障", "anomaly": "成本异动",
    "peer": "对标",
}


@app.get("/api/heatmap", summary="产品×月份×成本要素 热力图数据")
def heatmap(elements: str = Query("单位材料,单位人工,单位制造费用"),
            months: int = Query(6, ge=3, le=12),
            month: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Legacy compatibility implementation; original source materials are not included."""







    keys = [k.strip() for k in elements.split(",") if k.strip()]
    products = RF.list_products()
    out_products: List[Dict[str, Any]] = []
    all_vals: List[float] = []

    for p in products:
        as_of = month or max(r["月份"] for r in RF._read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv"))
        fs = RF.build(p, as_of)
        rows = fs.monthly[-months:]
        cells = []          # [要素, 月份, 环比%] —— ECharts heatmap 的格式
        for i, r in enumerate(rows):
            for k in keys:
                raw = r.get(k)
                if raw in (None, "", "—"):
                    continue
                cur = RF._num(raw)
                if cur is None:
                    continue
                # 环比 = (本月 - 上月) / 上月，需要上一个月同要素的值
                if i == 0:
                    continue                    # 首月没有上月，没有环比
                prev_raw = rows[i - 1].get(k)
                prev = RF._num(prev_raw) if prev_raw not in (None, "", "—") else None
                if not prev:
                    continue
                pct = round((cur - prev) / prev * 100, 2)
                cells.append([k, r["月份"], pct])
                all_vals.append(pct)
        out_products.append({
            "product": p, "cells": cells,
            "months": [r["月份"] for r in rows],
        })

    bound = round(max((abs(v) for v in all_vals), default=1.0), 2)
    return {
        "elements": keys, "products": out_products,
        "bound": bound,                     # 对称的色阶边界（±bound）
        "unit": "%",
        "note": "数值为逐月环比变动率；色阶以 0 为中心对称，便于一眼看出涨跌",
    }


@app.get("/api/decide", summary="自主决策：生成报告 or 只更新看板")
def decide_endpoint(month: str = Query("2026-05")) -> Dict[str, Any]:
    """Legacy compatibility implementation; original source materials are not included."""







    import decide as D
    d = D.decide(WIKI_ROOT, month)
    return d.to_dict()


@app.get("/api/graph", summary="知识图谱（节点与边）")
def graph(focus: str = Query("", description="只取该实体 N 跳内的子图，空=全图"),
          hops: int = Query(2, ge=1, le=4)) -> Dict[str, Any]:
    """
    返回图谱。`focus` 给定时只返回该实体 **hops 跳内的子图**。

    ⚠️ 全图 83 节点直接铺到前端会变成一团麻（力导向图节点一多就看不清）。
       按实体聚焦是**可读性**的必要条件，不是可选优化。
    """
    p = WIKI_ROOT / "graph" / "graph.json"
    if not p.exists():
        return {"ok": False, "error": "图谱尚未构建（跑 python scripts/graph_build.py）",
                "nodes": [], "edges": []}
    try:
        g = json.loads(p.read_text(encoding="utf-8"))
        import graph_build
        if (p.is_symlink() or not p.resolve().is_relative_to(WIKI_ROOT.resolve())
                or g.get("schema_version") != "pharma-graph/2"
                or g.get("source_scope") != "authorized_workspace"
                or g.get("source_signature") != graph_build.input_signature(WIKI_ROOT)):
            return {"ok": False, "error": "共享示例图谱缺少当前版本签名或源资料已变化，请重建后再查看。",
                    "nodes": [], "edges": [], "source_scope": "shared_example"}
    except (json.JSONDecodeError, OSError) as e:
        return {"ok": False, "error": f"图谱不可读：{type(e).__name__}",
                "nodes": [], "edges": []}

    nodes = {n["id"]: n for n in g.get("nodes", [])}
    edges = g.get("edges", [])

    if focus:
        if focus not in nodes:
            near = [i for i in nodes if focus and focus in i][:8]
            return {"ok": False, "error": f"图谱中没有「{focus}」",
                    "near": near, "nodes": [], "edges": []}
        # 双向 BFS 取子图
        keep, frontier = {focus}, {focus}
        for _ in range(hops):
            nxt = set()
            for e in edges:
                if e["src"] in frontier and e["dst"] not in keep:
                    nxt.add(e["dst"])
                if e["dst"] in frontier and e["src"] not in keep:
                    nxt.add(e["src"])
            keep |= nxt
            frontier = nxt
        edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]
        nodes = {i: n for i, n in nodes.items() if i in keep}

    return {
        "ok": True,
        "focus": focus or None,
        "as_of": g.get("as_of"),
        "source_signature": g["source_signature"],
        "source_scope": "shared_example",
        "stats": {"nodes": len(nodes), "edges": len(edges)},
        "type_zh": _GRAPH_TYPE_ZH,
        # 前端只需要 id/label/type 画点，边只留 src/dst/relation
        # 坐标（coord）带上——点开节点时用它跳到对应的知识页面
        "nodes": [{"id": n["id"], "label": n.get("label") or n["id"],
                   "type": n.get("type", ""), "coord": n.get("coord", ""),
                   "attrs": n.get("attrs") or {}} for n in nodes.values()],
        "edges": [{"src": e["src"], "dst": e["dst"],
                   "relation": e.get("relation", "")} for e in edges],
    }


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

@app.get("/api/benchmark", summary="对标分析三步法")
def benchmark(request: Request, product: str = Query(...), month: str = Query("2026-05"),
              llm: bool = Query(False, description="是否调用 LLM 生成第三步归因"),
              authorization: str = Header(default=""),
              lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    三步法：找差异 → 拆结构 → 拆原因。

    前两步是确定性计算（快，随时可跑）；第三步要调 LLM（慢，默认关闭）。
    看板默认只跑前两步，用户点「生成归因」时才带 `llm=1`。
    """
    info = _whoami(authorization, lw_sid) if llm else None
    if llm:
        _check_csrf(request, force=True)
    import benchmark_engine as BE
    import report_facts as RF

    # ⚠️ 先校验产品名。没有这道关，打错一个字（或客户端用 GBK 传中文）
    #    会匹配不到任何数据行，一路走到引擎深处才炸出晦涩的
    #    `KeyError: 'input'`——用户完全看不出是自己产品名写错了。
    known = RF.list_products()
    if product not in known:
        raise HTTPException(
            404, f"未知产品「{product}」。可选：{'、'.join(known)}")

    try:
        if llm:
            with _model_work(info):
                r = BE.run(product, month, use_llm=True, verbose=False)
        else:
            r = BE.run(product, month, use_llm=False, verbose=False)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback as _tb
        _tb.print_exc()
        raise HTTPException(500, f"三步法执行失败：{type(e).__name__}: {e}")
    s1, s2, s3 = r["steps"]
    return {
        "product": product, "month": month, "scope": "shared_pharma_sample",
        "steps_meta": r["steps_meta"],
        "step1": s1,
        "step2": s2,
        "step3": s3,
    }


# ---------------------------------------------------------------------------
# 对话 —— 这是看板**唯一**有副作用的端点
#
# ⚠️ 它写的是 **agent 的记忆（权威区）**，不是知识库。
#    wiki/ 与 raw/ 依然只读，看板不会因为聊天而改数据。
# ---------------------------------------------------------------------------

from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class ChatIn(BaseModel):
    question: str
    new: bool = False
    # 指定在哪个会话里继续（多会话切换）。**归属由服务端校验**：
    # 客户端只能"说出一个 id"，不能决定它属不属于自己。
    conversation_id: Optional[int] = None
    analysis_id: str = ""
    # ⚠️ 这里**故意没有 `user` 字段**。
    #    身份只能来自 Authorization 头，客户端无权自报。
    #    （旧版有 `user: str = "web"`，那是身份冒充的入口。）


def _session(user: str, new: bool = False,
             conversation_id: Optional[int] = None):
    """
    打开某用户的会话。

    ⚠️ 必须把**个人工作区**传下去。不传的话，AgentSession 只会读共享基线：
       用户上传的资料能编译进他的工作区，但**问答时检索不到**——
       上传功能实际是白做的。（这是本函数此前的真实缺陷。）

    `conversation_id` 指定切换到哪个已有会话。**先在这里验归属**，
    不通过直接 404——不把这个判断推给运行时，因为它不持有鉴权上下文。
    """
    import agent_runtime
    import paths
    ws = paths.ensure_workspace(user)          # 幂等建骨架
    # ⚠️ 两个根不能相同：个人工作区若等于代码仓库根，
    #    用户上传就能写进共享基线（越权）。这里显式拦一道。
    if Path(ws).resolve() == Path(WIKI_ROOT).resolve():
        raise HTTPException(500, "配置错误：个人工作区与共享基线指向同一目录")

    if conversation_id is not None:
        # 归属校验放在鉴权边界（这一层），与其余端点一致：不通过就是 404
        s = agent_runtime.AgentSession.open(
            user, root=WIKI_ROOT, personal=ws, new=new)
        if not s.mem.owns_conversation(s.user_id, conversation_id):
            raise HTTPException(404, f"会话不存在：{conversation_id}")

    return agent_runtime.AgentSession.open(
        user, root=WIKI_ROOT, personal=ws, new=new,
        conversation_id=conversation_id)


# ---------------------------------------------------------------------------
# 鉴权 —— 把"你是谁"从**客户端自报**改为**服务端认定**
#
# 修的是什么：原实现 `?user=demo` 这种参数是客户端随便填的，
# 改个名字就能读到那个人的全部对话历史与偏好（实测确认无任何校验）。
#
# 现在客户端只能**出示令牌**，说不出自己是谁：
#   Authorization: Bearer lw_xxx   →   auth.db 查表   →   得到用户标识
# 令牌对不上就 401，不存在"填个名字"这条路。
# ---------------------------------------------------------------------------


SESSION_COOKIE = "lw_sid"
CSRF_COOKIE = "lw_csrf"          # 刻意**不设 HttpOnly**：前端要读出来放进请求头
CSRF_HEADER = "X-CSRF-Token"

# 本地开发没有 HTTPS，`Secure` 会直接让 cookie 发不出去。
# ⚠️ 生产部署必须改成 True（见 references/技术方案.md 的部署章节）。
COOKIE_SECURE = os.getenv("LW_COOKIE_SECURE", "0") == "1"


def _whoami(authorization: str = Header(default=""),
            lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    认定当前用户。**两条路都认**，返回同一种身份结构。

      ① Cookie `lw_sid`      → 浏览器（人用；HttpOnly，JS 偷不到）
      ② `Authorization: Bearer lw_…` → 命令行 / 脚本 / 自动化截图

    ⚠️ 两路合一的好处：**13 个已有端点一行都不用改**——
       它们只认这个函数返回的身份，不关心凭证从哪来。

    保留 Bearer 的理由：`agent_auth.py --issue` 对 CI、自动化截图、
       外部集成仍有用，且改造成本为零。
    """
    import agent_auth
    a = agent_auth.Auth()
    if lw_sid:
        info = a.resolve_session(lw_sid)
        if info:
            return info
    tok = ""
    if authorization.lower().startswith("bearer "):
        tok = authorization[7:].strip()
    info = a.resolve(tok) if tok else None
    if not info:
        raise HTTPException(
            401, "请先登录。浏览器端用登录后的会话 Cookie；"
                 "命令行/脚本可带 `Authorization: Bearer <令牌>`"
                 "（令牌由 `python scripts/agent_auth.py --issue <用户>` 签发）。")
    return info


# ---- CSRF ----------------------------------------------------------------
# 用 Cookie 做会话就必然面对 CSRF：浏览器会自动带上本站 cookie，
# 所以"别的站点伪造一个表单"也能带着你的身份发请求。
#
# 三层防护（成本都很低，叠起来）：
#   ① Cookie 上的 `SameSite=Lax` —— 跨站 POST 不带 cookie（主力）
#   ② Origin/Referer 校验        —— 老旧浏览器不支持 SameSite 时的兜底
#   ③ Double-submit token        —— 防"同站子域被攻破"的情况
#
# ⚠️ 只对**写操作**（非 GET/HEAD/OPTIONS）检查：
#    GET 不应有副作用，检查它只会让公开数据接口无谓地变脆。

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _check_csrf(request: Request, force: bool = False) -> None:
    """写操作的来源校验。不通过就 403。"""
    if request.method in _SAFE_METHODS and not force:
        return

    # ② Origin / Referer 同源校验
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    host = request.headers.get("host") or ""
    if origin:
        # Origin 形如 http://127.0.0.1:8765
        if origin.rstrip("/").split("://")[-1] != host:
            raise HTTPException(403, "请求来源不合法（CSRF 防护）")
    elif referer:
        if referer.split("://")[-1].split("/")[0] != host:
            raise HTTPException(403, "请求来源不合法（CSRF 防护）")
    # 两者都没有：命令行工具（curl 等）不发这两个头。
    # 这类客户端用 Bearer 令牌，不依赖 cookie，所以不受 CSRF 影响——
    # 放行，否则 CLI 全用不了。

    # ③ Double-submit token（浏览器会带 cookie，故必然能比对）
    cookie_tok = request.cookies.get(CSRF_COOKIE, "")
    header_tok = request.headers.get(CSRF_HEADER, "")
    if request.cookies.get(SESSION_COOKIE) or cookie_tok:
        if not cookie_tok or not header_tok or not hmac.compare_digest(cookie_tok, header_tok):
            raise HTTPException(403, "缺少或错误的 CSRF 令牌，请刷新页面重试")


CSRF_EXEMPT = {"/api/auth/login", "/api/auth/register", "/api/auth/logout"}


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """
    写操作统一过 CSRF 检查。

    ⚠️ 三个 auth 端点是**豁免**的：
       登录/注册时用户还没有 csrf cookie——那个 cookie 正是**它们发下去的**，
       要求它等于"要求先登录才能登录"。豁免它们不降低安全性：
       攻击者伪造登录请求，最坏是"把受害者的浏览器登到攻击者的账号上"，
       而那需要攻击者知道自己的密码，且受害者能立刻察觉（界面变成了别人的）。
    """
    if request.url.path not in CSRF_EXEMPT:
        try:
            _check_csrf(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


def _set_session_cookies(resp: JSONResponse, sid: str) -> None:
    """下发会话 cookie + CSRF cookie。"""
    resp.set_cookie(
        SESSION_COOKIE, sid,
        max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
        secure=COOKIE_SECURE, path="/")
    # ⚠️ CSRF cookie 故意**不加 HttpOnly**——前端要读它放进 X-CSRF-Token。
    #    它本身不是凭证（攻击者拿到也做不了什么），配合服务端比对才有意义。
    resp.set_cookie(
        CSRF_COOKIE, secrets.token_urlsafe(24),
        max_age=60 * 60 * 24 * 30, httponly=False, samesite="lax",
        secure=COOKIE_SECURE, path="/")


def _client_ip(request: Request) -> str:
    """取客户端 IP。反代后面取 X-Forwarded-For 的第一段。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


# ---- 注册 / 登录 / 登出 ---------------------------------------------------

class CredIn(BaseModel):
    username: str
    password: str
    name: str = ""


@app.post("/api/auth/register", summary="注册新用户")
def auth_register(body: CredIn, request: Request) -> JSONResponse:
    """注册并**直接登录**（不用再输一遍）。"""
    import agent_auth
    import paths

    a = agent_auth.Auth()
    r = a.register(body.username, body.password, name=body.name)
    if not r["ok"]:
        raise HTTPException(400, r["error"])

    ext = r["external_id"]
    try:
        paths.ensure_workspace(ext)          # 建个人工作区（inbox/raw/wiki/state）
    except Exception:                        # noqa: BLE001
        pass                                 # 建不出来不影响注册，首次上传会再建

    sid = a.create_session(r["user_id"], ua=request.headers.get("user-agent", ""))
    resp = JSONResponse({
        "ok": True, "external_id": ext,
        "name": a.get_user(ext).get("name") or ext,
    })
    _set_session_cookies(resp, sid)
    return resp


@app.post("/api/auth/login", summary="登录")
def auth_login(body: CredIn, request: Request) -> JSONResponse:
    import agent_auth
    a = agent_auth.Auth()
    r = a.authenticate(body.username, body.password, ip=_client_ip(request),
                       ua=request.headers.get("user-agent", ""))
    if not r["ok"]:
        # 429 表示"被限流了"，与 401"凭证不对"区分开，前端好给不同提示
        raise HTTPException(429 if r.get("locked") else 401, r["error"])

    sid = a.create_session(r["user_id"], ua=request.headers.get("user-agent", ""))
    resp = JSONResponse({"ok": True, "external_id": r["external_id"],
                         "name": r["name"]})
    _set_session_cookies(resp, sid)
    return resp


@app.post("/api/auth/logout", summary="退出登录")
def auth_logout(lw_sid: str = Cookie(default="")) -> JSONResponse:
    import agent_auth
    if lw_sid:
        agent_auth.Auth().revoke_session(lw_sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


@app.get("/api/auth/me", summary="当前登录者（未登录也返回 200）")
def auth_me(authorization: str = Header(default=""),
            lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    当前登录者。

    ⚠️ **未登录时返回 200 + `logged_in: false`，不返回 401。**
       前端每次启动都调它；返回 401 会让浏览器控制台每次都留一条红色错误，
       用户看到会以为"网站坏了"。**未登录是一种正常状态，不是错误。**
    """
    import agent_auth
    import paths
    a = agent_auth.Auth()
    info = a.resolve_session(lw_sid) if lw_sid else None
    if not info and authorization.lower().startswith("bearer "):
        info = a.resolve(authorization[7:].strip())
    if not info:
        return {"logged_in": False}
    return {
        "logged_in": True,
        "external_id": info["external_id"],
        "name": info["name"],
        "role": info["role"],
        # ⚠️ 只回文件名，不回完整路径——路径属内部信息
        "user_db": paths.user_db(info["external_id"]).name,
    }


@app.get("/api/profile", summary="个人中心：账号 + 会话 + 个人知识库")
def profile(authorization: str = Header(default=""),
            lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    个人中心一屏所需的数据，**一次返回**。

    ⚠️ 为什么不拆成三个端点：个人中心开页会同时要账号、会话数、工作区清单。
       三次往返里任何一次慢或失败，页面就半残。合成一个原子快照更好。

    ⚠️ 数据全部取自**当前会话用户**——复用 `_session()`（按会话身份建库）
       与 `_ws()`（按会话身份定位工作区），两者都已在别处验证过隔离性。
    """
    import paths
    info = _whoami(authorization, lw_sid)
    ext = info["external_id"]

    out: Dict[str, Any] = {
        "external_id": ext,
        "name": info["name"],
        "role": info["role"],
        "user_db": paths.user_db(ext).name,
        "conversations": [], "workspace": None,
    }

    # 会话历史（复用已有的会话对象——它就是按 external_id 打开自己的库）
    try:
        s = _session(ext)
        convs = s.mem.list_conversations(s.user_id)
        out["conversations"] = [
            {"id": c["id"], "title": c.get("title") or "未命名",
             "created_at": c.get("created_at", "")} for c in convs
        ]
        out["stats"] = s.mem.stats()
    except Exception as e:  # noqa: BLE001
        out["conversations_error"] = f"{type(e).__name__}: {e}"

    # 个人工作区清单
    try:
        ws = _ws(ext)
        def _ls(sub: str) -> List[Dict[str, Any]]:
            d = ws / sub
            if not d.exists():
                return []
            return [{"path": p.relative_to(ws).as_posix(), "size": p.stat().st_size}
                    for p in sorted(d.rglob("*")) if p.is_file()
                    and not p.name.startswith(".")
                    and not (p.suffix.lower() == ".txt" and p.with_suffix(".pdf").exists())]
        raw, wiki, inbox = _ls("raw"), _ls("wiki"), _ls("inbox")
        cost_files = _ls("costing_datasets")
        raw += [file for file in cost_files if file["path"].endswith("/data.csv")]
        out["workspace"] = {
            "raw": raw, "wiki": wiki,
            # ⚠️ inbox（待编译）也要列。上传先落 inbox，跑完摄入才进 raw——
            #    不列的话，用户刚传完打开个人中心**什么都看不到**，
            #    会以为上传失败了（实测踩到）。
            "inbox": inbox,
            "counts": {"raw": len(raw), "wiki": len(wiki), "inbox": len(inbox)},
        }
    except Exception as e:  # noqa: BLE001
        out["workspace_error"] = f"{type(e).__name__}: {e}"

    return out


@app.get("/api/me", summary="当前令牌身份（命令行兼容）")
def me(authorization: str = Header(default=""),
       lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """保留旧路径：`_whoami` 现在两条路都认，行为与 `/api/auth/me` 一致但要求已登录。"""
    info = _whoami(authorization, lw_sid)
    import paths
    return {"external_id": info["external_id"], "name": info["name"],
            "role": info["role"],
            "user_db": paths.user_db(info["external_id"]).name}


@app.get("/api/chat/history", summary="当前用户的会话历史")
def chat_history(authorization: str = Header(default=""),
                 lw_sid: str = Cookie(default=""),
                 limit: int = Query(30, ge=1, le=200)) -> Dict[str, Any]:
    """
    当前令牌所属用户的会话列表 + 消息（供前端恢复界面）。

    ⚠️ **不再接受客户端传的 `user` 参数**——那是身份冒充的入口。
    """
    info = _whoami(authorization, lw_sid)
    s = _session(info["external_id"])
    convs = s.mem.list_conversations(s.user_id)
    msgs = [{"id": m["id"], "role": m["role"], "content": m["content"],
             "tool_name": m["tool_name"], "created_at": m["created_at"]}
            for m in s.mem.load(s.conv_id)]
    ctx = s.mem.get_context(s.conv_id)
    return {"me": {"external_id": info["external_id"], "name": info["name"]},
            "user_db": __import__("paths").user_db(info["external_id"]).name,
            "conversation_id": s.conv_id, "conversations": convs,
            "messages": msgs,
            "summary": ctx.get("summary", ""),
            "summary_upto": ctx.get("summary_upto", 0),
            "compactions": ctx.get("compactions", 0),
            "prefs": s.mem.all_prefs(s.user_id)}


# ---------------------------------------------------------------------------
# 会话管理（多会话：新建 / 切换 / 删除）
# ---------------------------------------------------------------------------
# ⚠️ **每个按 id 操作的端点都必须先校验归属**（`mem.owns_conversation`）。
#    跨用户读不到别人的数据是靠"一人一个物理库文件"挡住的，但**同一用户库内
#    有多个会话**——不校验归属，前端传一个别的 conv_id 就能读到同库内其他会话。
#
#    校验不过一律返回 **404 而不是 403**：403 等于告诉对方"这个 id 存在，
#    只是不属于你"，那是信息泄露（能据此枚举出别人有哪些会话 id）。
#    对调用方而言，"不存在"与"不是你的"应当是**同一种回答**。

def _conv_guard(info: Dict[str, Any], cid: int):
    """取某个会话，**先验归属**。不通过 → 404（见上方说明）。"""
    s = _session(info["external_id"])
    if not s.mem.owns_conversation(s.user_id, cid):
        raise HTTPException(404, f"会话不存在：{cid}")
    return s


@app.get("/api/conversations", summary="当前用户的会话列表")
def conversations_list(authorization: str = Header(default=""),
                       lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """只列会话本身，**不含消息**——列表要轻，消息在切进去时才拉。"""
    info = _whoami(authorization, lw_sid)
    s = _session(info["external_id"])
    return {"conversation_id": s.conv_id,        # 当前活跃的那个，前端用来选中
            "conversations": s.mem.list_conversations(s.user_id, limit=100)}


class ConvIn(BaseModel):
    title: str = ""


@app.post("/api/conversations", summary="新建会话")
def conversations_new(body: ConvIn = ConvIn(),
                      authorization: str = Header(default=""),
                      lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    显式新建一个会话。

    ⚠️ 前端**通常在首次提问时**才建（带 `new: true` 走 `/api/chat`），
       而不是点"新对话"按钮就建——否则用户点开看看又关掉，会攒一堆空会话。
       这个端点留给"想先建好再慢慢问"的场景，以及脚本/自动化。
    """
    info = _whoami(authorization, lw_sid)
    s = _session(info["external_id"])
    cid = s.mem.new_conversation(s.user_id, title=(body.title or "").strip()[:80])
    return {"conversation_id": cid}


@app.get("/api/conversations/{cid}", summary="取指定会话的消息与摘要")
def conversations_get(cid: int,
                      authorization: str = Header(default=""),
                      lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """切进某个会话时拉它的消息与压缩摘要。**先验归属。**"""
    info = _whoami(authorization, lw_sid)
    s = _conv_guard(info, cid)
    ctx = s.mem.get_context(cid)
    return {"conversation_id": cid,
            "messages": s.mem.load(cid),
            "summary": ctx.get("summary", ""),
            "summary_upto": ctx.get("summary_upto", 0),
            "compactions": ctx.get("compactions", 0)}


@app.delete("/api/conversations/{cid}", summary="删除会话")
def conversations_delete(cid: int,
                         authorization: str = Header(default=""),
                         lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    删除一个会话及其全部消息。**先验归属**（写操作，走 CSRF 中间件）。

    ⚠️ 消息由 `mem.delete_conversation()` 显式删除，不依赖外键级联——
       `PRAGMA foreign_keys` 是连接级开关，裸连接下默认关。
    """
    info = _whoami(authorization, lw_sid)
    s = _conv_guard(info, cid)
    ok = s.mem.delete_conversation(s.user_id, cid)
    return {"deleted": bool(ok), "conversation_id": cid}


@app.post("/api/chat", summary="智能问答（SSE 流式）")
def chat(body: ChatIn, authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> StreamingResponse:
    """
    SSE 流式对话。事件与 `agent_runtime.AgentSession.chat()` 一一对应：
        start / compact / tool_call / tool_result / token / done / error
    **前端不需要懂 agent 内部结构**——照着 type 渲染即可。

    ⚠️ 身份取自 `Authorization` 头，**不接受请求体里的 user**。
    """
    import json as _json
    info = _whoami(authorization, lw_sid)          # 令牌无效 → 401（在流开始前就返回）
    if not body.question.strip() or len(body.question) > 12000:
        raise HTTPException(422, "问题需要1—12000字的非空文本")
    if body.analysis_id:
        if len(body.question) > 2000:
            raise HTTPException(422, "当前分析问答最多2000字")
        import costing_outputs as outputs
        snapshot = _owned_snapshot(body.analysis_id, info)
        answer = outputs.answer_snapshot(snapshot, body.question)
        def snapshot_gen():
            yield _sse({"type": "start", "analysis_id": body.analysis_id, "context": snapshot["context"]})
            yield _sse({"type": "token", "text": answer.get("answer", answer.get("text", ""))})
            yield _sse({"type": "done", **answer, "analysis_id": body.analysis_id, "revision": snapshot["revision"]})
            yield "data: [DONE]\n\n"
        return StreamingResponse(snapshot_gen(), media_type="text/event-stream")
    release_model = _acquire_model_slot(info)

    def gen():
        try:
            s = _session(info["external_id"], new=body.new,
                         conversation_id=body.conversation_id)
            # 首轮提问时给会话起个能认出来的名字（纯脚本截取，不调 LLM）。
            # 只改仍是默认名的会话——用户/之前已命名过的不动。
            try:
                cur = [c for c in s.mem.list_conversations(s.user_id, limit=100)
                       if c["id"] == s.conv_id]
                if cur and (cur[0].get("title") or "") in ("", "成本分析问答"):
                    t = " ".join(body.question.split())[:24]
                    if t:
                        s.mem.set_title(s.conv_id, t)
            except Exception:  # noqa: BLE001
                pass          # 起标题失败不该影响问答本身
            for ev in s.chat(body.question):
                yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield ("data: " + _json.dumps({"type": "error",
                                           "message": f"{type(e).__name__}: {e}"},
                                          ensure_ascii=False) + "\n\n")
        finally:
            release_model()
        yield "data: [DONE]\n\n"

    return _ModelStreamingResponse(gen(), release_model=release_model, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# 已授权工作区的事实问答
#
# 当前 API 默认确定性脚本回答，不执行第二型号模型升级。
# 路由信号全部确定性，见 scripts/router.py。
# ---------------------------------------------------------------------------

class RouteIn(BaseModel):
    question: str
    analysis_id: str = ""


@app.post("/api/route", summary="已登录用户的证据问答（默认零模型调用）")
def route_ask(body: RouteIn, dry_run: bool = Query(False),
              authorization: str = Header(default=""),
              lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    登录后在当前用户工作区或指定核算快照内回答，并返回决策轨迹。
    当前接口以确定性事实生成回答，不发起模型调用。

    `dry_run=1` 只看路由裁决，不调模型——用于演示与测试。
    """
    info = _whoami(authorization, lw_sid)
    if not body.question.strip() or len(body.question) > 12000:
        raise HTTPException(422, "问题需要1—12000字的非空文本")
    if body.analysis_id:
        if len(body.question) > 2000:
            raise HTTPException(422, "当前分析问答最多2000字")
        import costing_outputs as outputs
        snapshot = _owned_snapshot(body.analysis_id, info)
        return {**outputs.answer_snapshot(snapshot, body.question), "analysis_id": body.analysis_id,
                "revision": snapshot["revision"], "context": snapshot["context"]}
    import router as RT
    if dry_run:
        d = RT.route(body.question, workspace_root=_ws(info["external_id"]))
        return {"level": d.level, "trace": [d.explain()], "dry_run": True}
    with _model_work(info):
        return RT.answer(body.question, verbose=False, workspace_root=_ws(info["external_id"]))


# ---------------------------------------------------------------------------
# 控制面板：个人工作区（上传 → 摄入 → 我的词条）
#
# ⚠️ 这里的写操作**只落在当前用户自己的工作区**（`data/users/<id>/ws/`）。
#    共享基线（代码仓库里的 wiki/ 与 raw/）**永不被用户上传改动**——
#    这是"唯一事实源"的底线。
#
# 摄入是**长任务**（LLM 编译 30-120 秒），故：
#    POST /api/control/ingest  →  SSE 流式返回进度（不是一个转圈等到超时）
# 与 `/api/chat` 同一套 SSE 模式，前端解析逻辑可复用。
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import uuid  # noqa: E402

from fastapi import File, Form, UploadFile  # noqa: E402

# 允许上传的后缀 —— 与 `ingest_raw.py` 的采集能力一致，且**白名单**而非黑名单
ALLOWED_EXT = {".md", ".txt", ".csv", ".xlsx", ".xls", ".docx", ".pdf", ".json"}
MAX_UPLOAD_BYTES = 40 * 1024 * 1024          # 单文件 40MB
MAX_FILES_PER_REQ = 200                       # 单次请求文件数上限


def _ws(external_id: str) -> Path:
    import paths
    return paths.ensure_workspace(external_id)


@app.get("/api/control/workspace", summary="个人工作区清单")
def control_workspace(authorization: str = Header(default=""), lw_sid: str = Cookie(default=""),
                      documents_only: bool = False) -> Dict[str, Any]:
    """当前用户的个人工作区清单（上传的资料 + 编译出的词条）。"""
    info = _whoami(authorization, lw_sid)
    ws = _ws(info["external_id"])

    def listing(sub: str) -> List[Dict[str, Any]]:
        d = ws / sub
        if not d.exists():
            return []
        if d.is_symlink() or not d.resolve().is_relative_to(ws.resolve()):
            raise HTTPException(400, "资料目录无效或越出个人工作区")
        out = []
        for p in sorted(d.rglob("*")):
            if (not p.is_file() or p.is_symlink()
                    or any(part.startswith(".") for part in p.relative_to(d).parts)
                    or not p.resolve().is_relative_to(d.resolve())):
                continue
            if p.suffix.lower() == ".txt" and p.with_suffix(".pdf").exists():
                continue                        # PDF 派生缓存，不重复列
            out.append({"path": p.relative_to(ws).as_posix(),
                        "size": p.stat().st_size,
                        "mtime": int(p.stat().st_mtime)})
        return out

    raw, wiki = listing("raw"), listing("wiki")
    if not documents_only:
        managed = [file for file in listing("costing_datasets")
                   if file["path"].endswith("/data.csv")
                   or "/sources/" in file["path"] and file["path"].endswith(".csv")]
        raw += [{**file, "read_only": True, "managed_by": "costing"} for file in managed]
    return {"external_id": info["external_id"], "workspace": str(ws.name),
            "raw": raw, "wiki": wiki,
            "counts": {"raw": len(raw), "wiki": len(wiki)}}


@app.post("/api/control/upload", summary="上传文件到个人工作区")
async def control_upload(authorization: str = Header(default=""),
                         lw_sid: str = Cookie(default=""),
                         files: List[UploadFile] = File(default=[]),
                         paths: str = Form(default="")) -> Dict[str, Any]:
    """
    收文件到**个人工作区**的 inbox/。

    用 multipart 而非 base64：文件可达几十 MB，base64 会膨胀 33% 且更吃内存。
    `paths` 是前端传的、与 files 一一对应的相对路径（选文件夹时保留目录语义），
    只用来做**提示**，实际落位仍由采集器按内容推断主题。
    """
    import json as _json
    info = _whoami(authorization, lw_sid)
    ws = _ws(info["external_id"])

    if len(files) > MAX_FILES_PER_REQ:
        raise HTTPException(400, f"单次最多 {MAX_FILES_PER_REQ} 个文件，收到 {len(files)}")

    try:
        rel_paths = _json.loads(paths) if paths else []
    except _json.JSONDecodeError:
        rel_paths = []
    if not isinstance(rel_paths, list):
        rel_paths = []

    inbox = ws / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    saved, rejected = [], []
    for i, uf in enumerate(files):
        # ⚠️ 文件名只取 basename —— 防止 `../../etc/passwd` 写到工作区外
        name = Path(uf.filename or "").name.strip()
        if not name:
            rejected.append({"file": uf.filename or "(无名)", "reason": "文件名为空"})
            continue
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            rejected.append({"file": name,
                             "reason": f"不支持的类型 {ext or '(无后缀)'}；"
                                       f"允许：{'、'.join(sorted(ALLOWED_EXT))}"})
            continue
        data = await uf.read()
        if len(data) > MAX_UPLOAD_BYTES:
            rejected.append({"file": name,
                             "reason": f"超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"})
            continue
        # 重名时加序号后缀，不覆盖（工作区里宁可多留一份也别静默替换）
        dest = inbox / name
        if dest.exists():
            dest = inbox / f"{Path(name).stem}_{uuid.uuid4().hex[:6]}{ext}"
        dest.write_bytes(data)
        saved.append({"name": dest.name, "size": len(data),
                      "hint_path": rel_paths[i] if i < len(rel_paths) else ""})

    return {"saved": saved, "rejected": rejected,
            "inbox": saved and str((inbox).name) or "",
            "message": (f"已接收 {len(saved)} 个文件"
                        + (f"，拒绝 {len(rejected)} 个" if rejected else ""))}


def _sse(obj: Dict[str, Any]) -> str:
    import json as _json
    return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/control/ingest", summary="对个人工作区跑摄入（SSE 流式）")
def control_ingest(authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> StreamingResponse:
    """
    对个人工作区跑一次摄入（采集 → 独立 LLM 编译 → 个人收尾）。SSE 流式返回。

    ⚠️ 整个过程**只读写该用户的工作区**：
       - `--wiki-root` 指向工作区 ⇒ raw/wiki 的写入都落在那里
       - 收尾 **skip db 步** ⇒ 不往共享 SQLite 镜像写个人数据
       - 规范仍从代码仓库读 ⇒ 用户改不了规则
    """
    info = _whoami(authorization, lw_sid)
    ws = _ws(info["external_id"])
    here = Path(__file__).resolve().parent.parent
    release_model = _acquire_model_slot(info)

    def gen():
        proc = None
        cmd = [sys.executable, str(here / "scripts" / "ingest_agent.py"),
               "--inbox", "--wiki-root", str(ws), "--quiet"]
        try:
            yield _sse({"type": "start", "workspace": ws.name,
                        "message": "开始处理你上传的资料"})
            # 用子进程而非同进程 import：摄入本来是"独立实例"设计，
            # 且它崩溃不该拖垮看板服务。
            # ⚠️ 必须显式沿用当前的数据根，否则子进程会用默认根，
            #    个人工作区就找不到了。
            env = dict(os.environ)
            env["LLM_WIKI_WS"] = str(ws)
            env["PYTHONIOENCODING"] = "utf-8"
            # 子进程必须沿用**当前生效的数据根**，否则它会退回默认根，
            # 于是 `--wiki-root` 指向的个人工作区就找不到了。
            # 直接继承 os.environ 已足够（服务本身也是从环境变量取根的）。
            # （数据根已由 os.environ 继承，无需另行设置）
            proc = subprocess.Popen(
                cmd, cwd=str(here), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            n = 0
            for line in proc.stdout:            # 逐行转发 —— agent 本来就在 print 进度
                s = (line or "").rstrip()
                if not s:
                    continue
                n += 1
                yield _sse({"type": "log", "line": s[:300]})
            proc.wait()
            ok = (proc.returncode == 0)
            yield _sse({"type": "done", "ok": ok, "returncode": proc.returncode,
                        "lines": n,
                        "message": ("摄入完成，已更新你的个人知识库" if ok
                                    else f"摄入中断（退出码 {proc.returncode}）。"
                                         f"已落盘的改动保留在工作区，可重跑。")})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            release_model()
        yield "data: [DONE]\n\n"

    return _ModelStreamingResponse(gen(), release_model=release_model, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.delete("/api/control/workspace", summary="删除个人工作区文件")
def control_delete(path: str = Query(..., description="相对工作区的路径"),
                   authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """删除个人工作区里的一个文件（资料或词条）。**不碰共享基线。**"""
    info = _whoami(authorization, lw_sid)
    from costing_workbench import document_target
    try:
        target = document_target(_ws(info["external_id"]), path)
    except PermissionError:
        raise HTTPException(403, "此路径由核算与报告功能管理，无法在资料入口删除") from None
    except (ValueError, OSError):
        raise HTTPException(400, "资料路径无效或越出当前工作区") from None
    if not target.exists():
        raise HTTPException(404, f"不存在：{path}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"deleted": path}


# ---------------------------------------------------------------------------
# 文档导出（Word / PDF）
# ---------------------------------------------------------------------------

class ExportIn(BaseModel):
    md: str = ""              # 源 markdown 的**名称**（限以下白名单目录内）
    kind: str = "report"
    fmt: str = "both"         # docx | pdf | both
    title: str = ""
    charts: bool = False


class GenerateIn(BaseModel):
    """Legacy compatibility implementation; original source materials are not included."""









    product: str
    month: str = "2026-06"
    theme: str = "monthly"
    period: str = ""
    dataset_id: str = "pharma-sample-v1"
    use_llm: bool = True


@app.get("/api/themes", summary="可选的分析主题")
def themes() -> Dict[str, Any]:
    """供前端渲染主题选择器——**选项由服务端给**，不在前端硬编。"""
    import period_agg as PA
    import report_build as RB
    return {
        "themes": [{"id": t, "name": RB.THEME_ZH[t],
                    "period_label": {"monthly": "分析月份",
                                     "quarterly": "分析季度",
                                     "topic": "专题对象"}[t],
                    "period_options": (
                        PA.AVAILABLE_QUARTERS if t == "quarterly"
                        else (RB.TOPIC_ELEMENTS if t == "topic" else []))}
                   for t in ("monthly", "quarterly", "topic")
                   if RB.TEMPLATES[t].is_file()],
    }


@app.post("/api/control/generate", summary="按分析主题生成报告")
def control_generate(body: GenerateIn,
                     authorization: str = Header(default=""),
                     lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    生成一份报告（不调 LLM 之外的副作用；产物落在 `reports/`）。

    生成与导出均须登录。use_llm 控制是否启用模型辅助；启用时仅允许
    deepseek-flash，核算和事实来源绑定仍由脚本完成。
    """
    info = _whoami(authorization, lw_sid)
    if body.dataset_id != "pharma-sample-v1":
        raise HTTPException(422, "该兼容入口只生成药企共享样例报告；其他数据集请基于当前分析结果保存报告")
    if body.theme not in ("monthly", "quarterly", "topic"):
        raise HTTPException(400, f"未知分析主题：{body.theme}")
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", body.month):
        raise HTTPException(422, "month必须是有效月份YYYY-MM")
    if body.product not in RF.list_products():
        raise HTTPException(422, "产品不属于该药企共享数据集")
    import period_agg as PA

    if body.theme == "quarterly":
        q = body.period or _quarter_of_month(body.month)
        if q not in PA.AVAILABLE_QUARTERS:
            raise HTTPException(400, f"该季度无数据：{q}（可选 {PA.AVAILABLE_QUARTERS}）")
        last_month = q[:4] + "-" + f"{int(q[-1]) * 3:02d}"
        if body.month < last_month:
            raise HTTPException(422, f"{q}需要至少截至{last_month}的完整资料，不能读取所选截止月之后的数据")
        out_name = f"{body.product}_{q}.md"
    elif body.theme == "topic":
        if not body.period:
            raise HTTPException(400, "专题分析需要指定对象（如「直接材料」）")
        out_name = f"{body.product}_{body.month}_专题{body.period}.md"
    else:
        out_name = f"{body.product}_{body.month}.md"

    import report_build as RB
    if body.theme == "topic" and body.period not in RB.TOPIC_ELEMENTS:
        raise HTTPException(422, "专题对象不在当前数据集支持范围内")
    try:
        with _model_work(info) if body.use_llm else nullcontext():
            r = RB.build(body.product, body.month, use_llm=body.use_llm, verbose=False,
                         theme=body.theme, period=body.period, workspace_root=WIKI_ROOT)
    except HTTPException:
        raise
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, "生成前校验失败：" + str(exc)) from exc
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"生成失败：{type(e).__name__}: {e}")

    import costing_outputs as outputs
    captured = outputs.capture_legacy_evidence(r.get("stats", {}).get("export_facts") or {},
                                               source_root=WIKI_ROOT, workspace_root=_ws(info["external_id"]))
    report_markdown = r["report"] + "\n\n" + captured.get("markdown", "")
    out_name = Path(out_name).stem + "_" + secrets.token_hex(6) + ".md"
    out = _ws(info["external_id"]) / "reports" / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"report_id": out.stem, "owner_external_id": info["external_id"],
                "product": body.product, "period": q if body.theme == "quarterly" else body.month,
                "as_of": body.month, "topic": body.period if body.theme == "topic" else "", "theme": body.theme,
                "dataset_id": body.dataset_id, "scenario": "actual", "scope": "shared_sample",
                "export_facts": r.get("stats", {}).get("export_facts"),
                "source_revision": captured.get("source_revision"),
                "evidence": captured.get("evidence", []), "fact_bindings": captured.get("fact_bindings", {}),
                "rpa_supported": True, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    temporary_md = out.with_name("." + out.name + ".tmp")
    temporary_meta = temporary_md.with_name(temporary_md.name + ".meta")
    try:
        temporary_md.write_text(report_markdown, encoding="utf-8", newline="\n")
        temporary_meta.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary_meta, out.with_name(out.name + ".meta.json"))
        os.replace(temporary_md, out)
    finally:
        temporary_md.unlink(missing_ok=True)
        temporary_meta.unlink(missing_ok=True)
    return {"ok": True, "file": out_name, "theme": body.theme,
            "metadata": _report_summary_metadata(metadata),
            "stats": {key: value for key, value in r["stats"].items() if key != "export_facts"}}


def _quarter_of_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"


@app.get("/api/control/exports", summary="可导出的文档清单")
def control_exports(authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """列出可导出的报告。"""
    info = _whoami(authorization, lw_sid)
    records = []
    for root, scope in ((_ws(info["external_id"]) / "reports", "personal"), (REPORTS, "shared_sample")):
        for path in sorted(root.glob("*.md")):
            if path.is_file() and not path.is_symlink():
                record = _report_record(path, scope)
                record["metadata"] = _report_summary_metadata(record["metadata"])
                records.append(record)
    return {"reports": records}


def _report_summary_metadata(metadata):
    return {**{key: value for key, value in metadata.items() if key not in {"export_facts", "evidence", "fact_bindings"}},
            "evidence_count": len(metadata.get("evidence", [])),
            "has_frozen_facts": bool(metadata.get("export_facts"))}


@app.get("/api/control/report-evidence", summary="已保存报告的冻结来源与原始行列")
def control_report_evidence(report: str, evidence_id: str = "", user=Depends(_require_costing_user)):
    _path, record = _find_report(report, user)
    metadata = record["metadata"]
    evidence = metadata.get("evidence", [])
    if evidence_id:
        found = next((item for item in evidence if item.get("evidence_id") == evidence_id), None)
        if found is None:
            raise HTTPException(404, "该来源不属于此报告")
        return {"report": report, "context": _report_summary_metadata(metadata), "evidence": found}
    return {"report": report, "context": _report_summary_metadata(metadata),
            "markdown": _path.read_text(encoding="utf-8"),
            "evidence": [{key: item.get(key) for key in ("evidence_id", "source_uri", "field", "selector", "status")}
                         for item in evidence], "fact_bindings": metadata.get("fact_bindings", {})}


@app.post("/api/control/export", summary="导出 Word/PDF")
def control_export(body: ExportIn, authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> FileResponse:
    """
    导出报告为 Word / PDF。**同步返回文件**（导出通常 3-15 秒，可接受）。

    ⚠️ 源文件必须在**白名单目录**内（reports/），
       且文件名只取 basename —— 否则 `../../.env` 之类能把任意文件读走。
    """
    import export_doc
    info = _whoami(authorization, lw_sid)

    if body.kind != "report":
        raise HTTPException(400, f"未知的导出类型：{body.kind}")
    # ⚠️ 先挡空名字：`REPORTS / Path("").name` == `REPORTS / ""` == **目录本身**，
    #    而目录也 `exists()`，于是守卫放行、下一步读目录崩成 500。
    #    改判 `is_file()` 并显式拒绝空名，让缺参数的请求得到 400 而不是 500。
    src, record = _find_report(body.md, info)
    if body.fmt not in ("docx", "pdf", "both"):
        raise HTTPException(400, "fmt 只能是 docx / pdf / both")

    import tempfile
    exports_root = _ws(info["external_id"]) / "exports"
    exports_root.mkdir(parents=True, exist_ok=True)
    report_key = re.sub(r"[^0-9A-Za-z._-]", "_", str(record["metadata"].get("report_id") or src.stem))[:48]
    # Every request owns its markdown, chart, document and ZIP intermediates.
    # Keeping them in a fresh directory also preserves all earlier exports.
    out_dir = Path(tempfile.mkdtemp(prefix="report-" + report_key + "-", dir=exports_root))
    try:
        if record.get("analysis_id"):
            import costing_outputs as outputs
            snapshot = _owned_snapshot(record["analysis_id"], info)
            outs = [outputs.export_snapshot(snapshot, fmt, out_dir)
                    for fmt in (["docx", "pdf"] if body.fmt == "both" else [body.fmt])]
        else:
            if record["theme"] not in {"monthly", "quarterly", "topic"}:
                raise HTTPException(422, "该历史报告缺少已验证的主题/期间元数据，不能按月报导出")
            outs = export_doc.export(src, body.fmt, out_dir,
                                     title=body.title or src.stem,
                                     with_charts=body.charts, context=record["metadata"])
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"导出失败：{type(e).__name__}: {e}")

    if body.fmt == "both":
        import zipfile
        bundle = out_dir / (src.stem + ".zip")
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in outs:
                archive.write(path, path.name)
        return FileResponse(str(bundle), media_type="application/zip", filename=bundle.name)
    pick = next((p for p in outs if p.suffix == ".pdf"), outs[0])
    media = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "pdf": "application/pdf"}[pick.suffix.lstrip(".")]
    return FileResponse(str(pick), media_type=media, filename=pick.name)


# ---------------------------------------------------------------------------

#

#    mock 没起时不假装成功——如实返回 degraded，前端显示"未送达"。
# ---------------------------------------------------------------------------

class DispatchIn(BaseModel):
    report: str = ""          # 报告文件名（限 reports/ 内）
    month: str = ""           # 旧客户端兼容：不用于推断报告身份
    product: str = ""


def _rpa_client(info):
    import rpa_client
    return rpa_client.RPAClient(outbox_dir=_ws(info["external_id"]) / "rpa_outbox")


@app.get("/api/rpa/health", summary="模拟 RPA 服务探活")
def rpa_health() -> Dict[str, Any]:
    """模拟 RPA 服务探活。前端据此显示"下游系统已连接/未连接"。"""
    import rpa_client
    return rpa_client.RPAClient().health()


@app.get("/api/rpa/stats", summary="下游工单系统整体统计")
def rpa_stats(authorization: str = Header(default=""),
              lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """下游服务的总数 / 按状态 / 按优先级 / 通知数。"""
    info = _whoami(authorization, lw_sid)
    client = _rpa_client(info)
    listing = client.list_all_tasks(owner_external_id=info["external_id"])
    if not listing.get("ok"):
        return listing
    tasks = client.extract_tasks(listing)
    counts = client.summarize_status(tasks)
    return {"ok": True, "simulation": True,
            "data": {"total_tasks": counts["generated"], "by_status": counts["by_status"],
                     "by_priority": {priority: sum(task.get("priority") == priority for task in tasks)
                                     for priority in ("high", "medium", "low")}}}


@app.get("/api/rpa/task/{task_id}", summary="单个整改任务详情（含状态流转）")
@app.get("/api/rpa/tasks/{task_id}", summary="工单追踪地址兼容入口")
def rpa_task(task_id: str, authorization: str = Header(default=""),
             lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    查单个任务。

    ⭐ 比列表查询多的是 **`status_history`**——任务的状态流转时间线。
       列表只给"当前是什么状态"，这里给"什么时候变的"。
    """
    info = _whoami(authorization, lw_sid)
    response = _rpa_client(info).get_task(task_id)
    if response.get("ok") and response.get("task", {}).get("source", {}).get("owner_external_id") != info["external_id"]:
        raise HTTPException(404, "该任务不存在或不属于当前账户")
    return response


class NotifyIn(BaseModel):
    recipient: str
    department: str = ""
    message: str


@app.post("/api/rpa/notify", summary="手动补发微信通知")
def rpa_notify(body: NotifyIn, authorization: str = Header(default=""),
               lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    **补发**一条微信通知。

    ⚠️ 正常流程用不到它——派发任务时会自动发通知。
       这个端点只在"自动通知没送达"时人工补发。
    """
    _whoami(authorization, lw_sid)
    import rpa_client
    return rpa_client.RPAClient().notify(body.recipient, body.department,
                                         body.message)


@app.get("/api/rpa/tasks", summary="整改任务追踪看板数据")
def rpa_tasks(authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """Legacy compatibility implementation; original source materials are not included."""



    info = _whoami(authorization, lw_sid)
    import rpa_client
    c = _rpa_client(info)
    r = c.list_all_tasks(owner_external_id=info["external_id"])
    if not r["ok"]:
        return {"ok": False, "error": r["error"], "tasks": [],
                "counts": {"generated": 0, "delivered": 0, "confirmed": 0},
                "pending": c.pending_tasks(), "simulation": True}
    tasks = c.extract_tasks(r)
    counts = c.summarize_status(tasks)
    return {"ok": True, "tasks": tasks, "counts": counts, "pending": c.pending_tasks(), "simulation": True,
            "status_zh": rpa_client.STATUS_ZH}


@app.post("/api/rpa/dispatch", summary="下发整改任务到责任人")
def rpa_dispatch(body: DispatchIn, authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    把某份报告第六章的「整改任务清单」派发到模拟 RPA 服务。

    ⚠️ **不从零生成任务** —— 直接搬报告 6.4 表格里已有的任务。
    否则会出现"报告里写的"与"实际发出去的"不一致，那是数据源分裂。
    """
    info = _whoami(authorization, lw_sid)
    import rpa_client
    import hashlib

    # 同 control_export：空 `report` 会让 `REPORTS / ""` 退化成目录本身，
    # 只查 exists() 会放行、随后读目录崩成 500。先挡空名、再判 is_file()。
    src, record = _find_report(body.report, info)
    metadata = record["metadata"]
    if not metadata.get("rpa_supported") or not metadata.get("product") or not metadata.get("period"):
        raise HTTPException(422, "该报告缺少已验证的整改任务及产品/期间元数据，不能派发")
    if (body.product and body.product != metadata["product"]) or (body.month and body.month != metadata["period"]):
        raise HTTPException(422, "请求的产品/期间与报告元数据不一致")
    markdown = src.read_text(encoding="utf-8")
    revision = metadata.get("revision") or hashlib.sha256(markdown.encode("utf-8")).hexdigest()

    payloads = rpa_client.RPAClient.parse_report_tasks(
        markdown, metadata.get("as_of") or metadata["period"], metadata["product"])
    if not payloads:
        raise HTTPException(400, "该报告的 6.4 整改任务清单为空，无可派发任务")

    namespace = hashlib.sha256((info["external_id"] + "|" + metadata["report_id"] + "|" + revision).encode("utf-8")).hexdigest()[:12]
    for payload in payloads:
        original_id = payload["task_id"]
        payload["task_id"] = original_id + "-" + namespace
        payload["source"].update({"owner_external_id": info["external_id"],
                                  "dataset_id": metadata.get("dataset_id", ""),
                                  "report_id": metadata["report_id"], "revision": revision,
                                  "analysis_period": metadata["period"], "analysis_theme": metadata["theme"],
                                  "report_task_id": original_id})
    rep = _rpa_client(info).dispatch(payloads)
    return {**rep, "report_metadata": metadata, "revision": revision}


@app.post("/api/rpa/retry", summary="手动重试当前账户未送达的模拟任务")
def rpa_retry(user=Depends(_require_costing_user)):
    return _rpa_client(user).retry_pending()


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(STATIC / "index.html"))


def main() -> int:
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(description="基于 llm-wiki 的智能分析面板 后端（只读）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reload", action="store_true",
                    help="改代码自动重启（**仅开发用**；生产别带，见下）")
    a = ap.parse_args()

    print(f"看板: http://{a.host}:{a.port}/")
    print(f"文档: http://{a.host}:{a.port}/docs")
    if a.reload:
        # ⚠️ 为什么要有这个开关（血泪）：
        #    `python server/app.py` 不带 --reload 时，进程跑的是**启动那一刻**的代码。
        #    实测踩过：改了 app.py 后没重启，接口行为一直是旧的，
        #    于是对着真实的旧服务反复调试"为什么 401"——查了令牌哈希、库路径、
        #    盘符、解释器，全对，白花好几轮。**陈旧进程会制造假象证据。**
        #
        # ⚠️ uvicorn 的 reload 要求传**导入字符串**而不是 app 对象，
        #    否则它无法在子进程里重新导入。所以要把它能看到 app 的目录
        #    （server/）加进 sys.path，再传 "app:app"。
        #    reload_dirs 限定在 server/，免得编辑器保存 reports/ 之类也触发重启。
        print("（开发模式：改 server/ 下的代码会自动重启）")
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        uvicorn.run("app:app", host=a.host, port=a.port, log_level="info",
                    reload=True, reload_dirs=[os.path.dirname(os.path.abspath(__file__))])
    else:
        uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())