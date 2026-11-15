"""Opt-in object-only projection from authenticated, pinned costing snapshots.

This module is not a share link, permission system, endorsement or digital
signature. The caller must supply the already authenticated owner and
workspace, never values selected by the request body. No default report,
snapshot writer, exporter or public application route calls this module.
"""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from hmac import compare_digest
import json
from pathlib import Path
import re
import unicodedata

from costing_outputs import load_snapshot

SCHEMA_VERSION = "costing-object-only-summary/1"
SUMMARY_SCOPE = "object_only_v1"
_BASIS = "stored_final_object_metrics_no_source_dossier"
_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_MONEY = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_CURRENCY = re.compile(r"[A-Z]{3}")
_FIELDS = ("object_id", "name", "kind", "quantity", "quantity_unit", "total_cost",
           "unit_cost", "revenue", "revenue_status", "operating_margin")
_ROOT = {"schema_version", "summary_scope", "basis", "period", "currency", "scenario", "object"}
_SCENARIOS = ("actual", "budget", "standard", "forecast")
_REVENUE_STATES = ("not_applicable", "complete", "missing")
_MAX_NUMBER_CHARS = 160


class ObjectSummaryError(ValueError):
    """The pinned source or structurally valid requested view is unavailable."""


def _bounded_text(value, limit):
    return (type(value) is str and 0 < len(value) <= limit and value == value.strip()
            and not any(unicodedata.category(char).startswith("C") for char in value))


def _number(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not str or len(value) > _MAX_NUMBER_CHARS or not _MONEY.fullmatch(value):
        raise ObjectSummaryError("Object-only summary requires bounded decimal strings")
    return Decimal(value)


def _payload_ok(value):
    if (type(value) is not dict or set(value) != _ROOT or value["schema_version"] != SCHEMA_VERSION
            or value["summary_scope"] != SUMMARY_SCOPE or value["basis"] != _BASIS):
        raise ObjectSummaryError("Invalid object-only summary format")
    period, currency, scenario, obj = (value[k] for k in ("period", "currency", "scenario", "object"))
    if (type(period) is not str or not _MONTH.fullmatch(period) or period.startswith("0000")
            or type(currency) is not str or not _CURRENCY.fullmatch(currency)
            or type(scenario) is not str or scenario not in _SCENARIOS
            or type(obj) is not dict or set(obj) != set(_FIELDS)):
        raise ObjectSummaryError("Invalid object-only summary scope")
    for field, limit in (("object_id", 128), ("name", 256), ("kind", 64), ("quantity_unit", 64)):
        if not _bounded_text(obj[field], limit):
            raise ObjectSummaryError("Invalid selected object caption or unit")
    _number(obj["total_cost"])
    qty = _number(obj["quantity"], optional=True)
    unit = _number(obj["unit_cost"], optional=True)
    revenue = _number(obj["revenue"], optional=True)
    margin = _number(obj["operating_margin"], optional=True)
    if (qty is not None and qty < 0) or ((unit is not None) != (qty is not None and qty > 0)):
        raise ObjectSummaryError("Inconsistent selected object quantity and unit cost")
    state = obj["revenue_status"]
    if type(state) is not str or state not in _REVENUE_STATES:
        raise ObjectSummaryError("Invalid selected object revenue state")
    # Preserve recorded figures exactly rather than recomputing a finalized
    # margin under a different Decimal context than the source costing run.
    if state == "complete":
        if revenue is None or revenue < 0 or margin is None:
            raise ObjectSummaryError("Inconsistent selected object revenue and margin")
    elif revenue is not None or margin is not None:
        raise ObjectSummaryError("Unavailable revenue never supplies a margin")


def _seal(payload):
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def verify_object_only_summary(receipt):
    """Raise on structural drift or altered bytes; SHA is not a signature."""
    if type(receipt) is not dict:
        raise ObjectSummaryError("Invalid object-only summary receipt")
    pin = receipt.get("revision")
    if type(pin) is not str or not _DIGEST.fullmatch(pin):
        raise ObjectSummaryError("Invalid object-only summary receipt")
    payload = {key: v for key, v in receipt.items() if key != "revision"}
    _payload_ok(payload)
    if not compare_digest(_seal(payload), pin):
        raise ObjectSummaryError("Object-only summary receipt did not match")


def project_object_only(analysis_id, workspace_root, *, expected_revision, expected_workspace_owner, object_id):
    """Return a new isolated summary; require inherited owner and exact source revision.

    Input is an already authorized local workspace, never an arbitrary user
    path. The original snapshot must be checked with its normal reader. The
    summary has its own digest and deliberately does not publish the source
    snapshot's ID, hash, owner, dataset, or links.
    """
    unavailable = "Pinned costing object is unavailable for this workspace"
    if (type(analysis_id) is not str or not _ID.fullmatch(analysis_id)
            or type(expected_revision) is not str or not _DIGEST.fullmatch(expected_revision)
            or not _bounded_text(expected_workspace_owner, 256) or not _bounded_text(object_id, 128)):
        raise ObjectSummaryError(unavailable)
    try:
        root = Path(workspace_root).resolve()
        # Avoid the normal reader's creation of a missing storage directory on a lookup miss.
        if not (root / "costing_snapshots" / (analysis_id + ".json")).is_file():
            raise FileNotFoundError
        snapshot = load_snapshot(analysis_id, root)
        context, result = snapshot["context"], snapshot["result"]
        if (not compare_digest(snapshot["revision"], expected_revision)
                or context.get("owner_external_id") != expected_workspace_owner
                or result.get("ledger_reconciled") is not True
                or any(context.get(k) != result.get(k) for k in ("dataset_id", "period", "scenario"))):
            raise ValueError
        scope, rows = result["scope"], result["objects"]
        ids = [row["object_id"] for row in rows]
        if (len(ids) != len(set(ids)) or scope["object_ids"] != sorted(ids)
                or scope["type"] not in ("dataset", "object")
                or (scope["type"] == "object" and (ids != [object_id] or context.get("cost_object") != object_id))
                or (scope["type"] == "dataset" and context.get("cost_object"))):
            raise ValueError
        source = next(row for row in rows if row["object_id"] == object_id)
        if source["currency"] != result["currency"]:
            raise ValueError
        payload = {"schema_version": SCHEMA_VERSION, "summary_scope": SUMMARY_SCOPE, "basis": _BASIS,
                   "period": result["period"], "currency": result["currency"], "scenario": result["scenario"],
                   "object": {field: source[field] for field in _FIELDS}}
        _payload_ok(payload)
    except (OSError, KeyError, ValueError, TypeError, RuntimeError, StopIteration, AttributeError):
        raise ObjectSummaryError(unavailable) from None
    return {**payload, "revision": _seal(payload)}
