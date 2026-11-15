"""User-scoped workbench records and configured synthetic example bundles."""
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import re

EXAMPLES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
CATALOG_NAME = "workbench_examples.json"
EXAMPLE_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
REVISION = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}\.csv\Z")


def _json_file(path, maximum=256 * 1024):
    data = path.read_bytes()
    if len(data) > maximum:
        raise ValueError("example configuration exceeds its size limit")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("example configuration must be an object")
    return value


def _folder(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid example folder")
    pieces = relative.split("/")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", part) for part in pieces):
        raise ValueError("invalid example folder")
    current = root
    for part in pieces:
        current = current / part
        if current.is_symlink():
            raise ValueError("example folders must be ordinary directories")
    folder = current.resolve()
    if not folder.is_relative_to(root.resolve()) or not folder.is_dir():
        raise ValueError("example folder is unavailable")
    return folder


def _entries(root):
    catalog = _json_file(root / CATALOG_NAME)
    entries = catalog.get("examples")
    if catalog.get("version") != 1 or not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        raise ValueError("invalid example catalog")
    by_id = {}
    allowed = {"id", "name", "description", "scope", "folder"}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != allowed:
            raise ValueError("invalid example entry")
        ident = entry["id"]
        if not isinstance(ident, str) or not EXAMPLE_ID.fullmatch(ident) or ident in by_id:
            raise ValueError("duplicate or invalid example identifier")
        if any(not isinstance(entry[key], str) or not entry[key].strip() or len(entry[key]) > 600
               for key in ("name", "description", "scope")):
            raise ValueError("example description is missing")
        by_id[ident] = entry
    return by_id


def get_example(ident, fixture_root=None):
    root = Path(fixture_root or EXAMPLES_ROOT)
    entries = _entries(root)
    if ident not in entries:
        raise LookupError("example not found")
    entry = entries[ident]
    folder = _folder(root, entry["folder"])
    for name in ("rules.json", "expected.json"):
        if (folder / name).is_symlink():
            raise ValueError("example files must be ordinary files")
    schema = _json_file(folder / "rules.json")
    expected = _json_file(folder / "expected.json")
    total = expected.get("total_cost")
    try:
        amount = Decimal(total)
    except (TypeError, InvalidOperation):
        raise ValueError("independent example total is unavailable") from None
    if not isinstance(total, str) or not amount.is_finite() or amount < 0:
        raise ValueError("independent example total is invalid")
    composite = schema.get("raw_composite")
    if not isinstance(composite, dict) or not isinstance(composite.get("scope"), dict):
        raise ValueError("example ledger scope is missing")
    scope = composite["scope"]
    period = schema.get("period") or scope.get("period")
    currency = schema.get("currency") or scope.get("currency")
    if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", str(period)) or currency != "CNY":
        raise ValueError("example period or currency is invalid")
    files, descriptions = {}, []
    hashes = composite.get("source_hashes", {})
    if not isinstance(hashes, dict):
        raise ValueError("invalid example source hashes")
    total_bytes = 0
    for path in sorted(folder.glob("*.csv")):
        if path.is_symlink() or not SOURCE_NAME.fullmatch(path.name):
            raise ValueError("invalid example source file")
        raw = path.read_bytes()
        total_bytes += len(raw)
        if len(raw) > 10 * 1024 * 1024 or total_bytes > 32 * 1024 * 1024:
            raise ValueError("example source exceeds its size limit")
        digest = hashlib.sha256(raw).hexdigest()
        if path.stem in hashes and hashes[path.stem] != digest:
            raise ValueError("example source hash mismatch")
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("invalid example source headers")
        count = 0
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("ragged example source")
            count += 1
        if not count:
            raise ValueError("empty example source")
        files[path.name] = raw
        descriptions.append({"name": path.name, "row_count": count, "sha256": digest})
    if not 1 <= len(files) <= 16:
        raise ValueError("example requires one to sixteen source files")
    metadata = {key: entry[key] for key in ("id", "name", "description", "scope")}
    metadata.update(period=period, currency=currency, expected_total=total,
                    data_kind="synthetic", available=True, source_files=descriptions,
                    configuration_version=schema.get("plugin_version", "1"),
                    configuration_sha256=hashlib.sha256((folder / "rules.json").read_bytes()).hexdigest())
    return metadata, schema, files


def list_examples(fixture_root=None):
    root = Path(fixture_root or EXAMPLES_ROOT)
    entries = _entries(root)
    examples, diagnostics = [], []
    for ident in entries:
        try:
            metadata, _, _ = get_example(ident, root)
            examples.append(metadata)
        except (ValueError, OSError, csv.Error) as exc:
            diagnostics.append({"id": ident, "name": entries[ident]["name"],
                                "status": "unavailable", "message": "示例资料暂不可用"})
    return {"examples": examples, "diagnostics": diagnostics,
            "basis": "synthetic_validation_examples"}


def personal_reports(workspace, owner, record_loader):
    reports = Path(workspace) / "reports"
    if not reports.exists():
        return [], []
    resolved = reports.resolve()
    if reports.is_symlink() or not resolved.is_relative_to(Path(workspace).resolve()):
        raise ValueError("report directory is outside the personal workspace")
    items, diagnostics = [], []
    for path in reports.glob("costing-*.md"):
        match = re.fullmatch(r"costing-([0-9a-f]{32})\.md", path.name)
        sidecar = path.with_name(path.name + ".meta.json")
        if not match:
            continue
        try:
            if (path.is_symlink() or sidecar.is_symlink()
                    or not path.is_file() or not sidecar.is_file()
                    or not path.resolve().is_relative_to(resolved)
                    or not sidecar.resolve().is_relative_to(resolved)):
                raise ValueError("invalid report file")
            item = record_loader(path, "personal")
            metadata = item["metadata"]
            revision = metadata.get("revision", "")
            if (metadata.get("theme") != "costing"
                    or metadata.get("analysis_id") != match[1]
                    or not isinstance(revision, str) or not REVISION.fullmatch(revision)
                    or metadata.get("owner_external_id") != owner
                    or metadata.get("workspace_id") != owner):
                raise ValueError("report identity could not be verified")
            saved = path.stat().st_mtime
            item = {**item, "scope": "personal", "revision": revision,
                    "saved_at": datetime.fromtimestamp(saved, timezone.utc).isoformat(),
                    "source_scope": metadata.get("scope", "unavailable"),
                    "cost_total": metadata.get("cost_total"),
                    "currency": metadata.get("currency"),
                    "object_count": metadata.get("object_count")}
            item["metadata"] = {key: value for key, value in metadata.items()
                                if key not in {"export_facts", "evidence", "fact_bindings"}}
            items.append(item)
        except (OSError, ValueError, KeyError, TypeError):
            diagnostics.append({"kind": "report", "status": "unavailable",
                                "message": "一份历史成本报告缺少可核对的身份或版本"})
    items.sort(key=lambda row: row["saved_at"], reverse=True)
    return items, diagnostics

def document_target(workspace, relative):
    """Resolve a supplementary-document operation inside its permitted folders."""
    root = Path(workspace).resolve()
    if not isinstance(relative, str) or not relative or "\x00" in relative or len(relative) > 2048:
        raise ValueError("invalid supplementary document path")
    supplied = root / relative.replace("\\", "/")
    target = supplied.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("document path leaves its workspace")
    if target.relative_to(root).parts[0] not in {"raw", "wiki", "inbox"}:
        raise PermissionError("file is managed by costing or reports")
    if supplied.is_symlink():
        raise ValueError("supplementary document cannot be a symbolic link")
    return target
