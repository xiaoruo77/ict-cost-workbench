"""Internal, read-only continuity preview. No upload route, provider or CAS writer."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from hashlib import sha256
import json
import re
import unicodedata
from typing import Protocol

from cost_core.inventory_eval import (
    MAX_HISTORY, MAX_STOCK_CENTS, MAX_STOCK_QUANTITY, instant, money, money_text, quantity, quantity_text,
)
from cost_core.inventory_transfer import transfer_key
from cost_core.object_rollups import safe_text
from costing_outputs import validate_snapshot_integrity


@dataclass(frozen=True, slots=True)
class SnapshotPin:
    analysis_id: str
    revision: str


@dataclass(frozen=True, slots=True)
class InventoryScope:
    owner: str
    book_uid: str
    book_label: str
    inventory_uid: str
    branch: str | None = None  # explicit only for a composite snapshot


@dataclass(frozen=True, slots=True)
class WarehouseBalance:
    warehouse: str
    item: str
    unit: str
    quantity: str
    carrying_value: str
    state_sha256: str


@dataclass(frozen=True, slots=True)
class TransitPosition:
    transfer_id: str
    from_warehouse: str
    to_warehouse: str
    item: str
    unit: str
    quantity: str
    carrying_value: str
    dispatch_at: str
    dispatch_source_id: str
    dispatch_evidence_sha256: str


@dataclass(frozen=True, slots=True)
class CarriedReceipt:
    transfer_id: str
    warehouse: str
    item: str
    unit: str
    quantity: str
    received_at: str
    sequence: int
    receipt_source_id: str
    dispatch_source_id: str
    receipt_evidence_sha256: str


@dataclass(frozen=True, slots=True)
class ContinuityBinding:
    """Authenticated registry view AND independently reconciled boundary claims.

    A real provider must establish provenance outside these files, and bind the
    exact previous and current scoped populations in one consistent read. This
    program will not create/publish that authority or grant access to its data.
    """
    scope: InventoryScope
    prior_pin: SnapshotPin
    current_period: str
    accepted_close_token: str
    policy_sha256: str
    warehouse_close_sha256: str
    boundary_attestation_sha256: str
    arrival_attestation_sha256: str
    transit: tuple[TransitPosition, ...]
    receipts: tuple[CarriedReceipt, ...]


class ContinuityAuthority(Protocol):
    def read_continuity(
        self, *, scope: InventoryScope, prior_pin: SnapshotPin, current_period: str,
    ) -> ContinuityBinding:
        """Return a binding only after trusted, independent, owner-scoped checks."""
        ...


class ContinuityUnavailable(ValueError):
    """Provider absent/failed, wrong scope, unavailable, or wrong revision."""


class ContinuityMismatch(ValueError):
    """Valid typed controls and supplied opening/arrival data do not reconcile."""


def _need(ok, reason):
    if not ok:
        raise ContinuityMismatch(reason)


def _hex(value, n=64):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{"+str(n)+r"}",value) is not None


def _sha(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,
                             separators=(",",":")).encode("utf-8")).hexdigest()


def _month_idx(p):
    _need(type(p) is str and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])",p)
          and not p.startswith("0000"),"invalid accounting month")
    a,b=map(int,p.split("-"))
    return a*12+b-1


def _end_before(current_period):
    return (instant(current_period+"-01T00:00:00Z")-timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text_fields(*values):
    return all(safe_text(x,128) for x in values)


def _source_identity(value):
    # Collision detection only: evidence and all exact row comparisons keep
    # the original ID. Do not log or include candidate IDs in error messages.
    _need(safe_text(value,512),"invalid source identity")
    key=unicodedata.normalize("NFKC",unicodedata.normalize("NFKC",value).casefold())
    _need(safe_text(key,2048),"invalid canonical source identity")
    return key


def _qty(v, *, zero=False):
    q=quantity(v,zero)
    _need(q<=MAX_STOCK_QUANTITY,"quantity outside current precision/capacity")
    return q


def _amount(v):
    c=money(v)
    _need(c<=MAX_STOCK_CENTS,"value outside current capacity")
    return c


def _warehouse_key(x):
    return (x.warehouse,x.item,x.unit)


def _warehouse_body(x):
    _need(type(x) is WarehouseBalance and _text_fields(*_warehouse_key(x))
          and _hex(x.state_sha256),"malformed warehouse close position")
    q,c=_qty(x.quantity,zero=True),_amount(x.carrying_value)
    _need(q or not c,"zero stock quantity with positive price")
    return {**asdict(x),"quantity":quantity_text(q),"carrying_value":money_text(c)}


def _warehouse_population(positions):
    _need(type(positions) is tuple and 0<len(positions)<=64,"invalid warehouse population")
    states={}
    for p in positions:
        body=_warehouse_body(p)
        k=_warehouse_key(p)
        _need(k not in states,"duplicate warehouse close position")
        states[k]=body
    return [states[key] for key in sorted(states)],set(states)


def _native_boundary(*, snapshot, prior_pin, scope, current_period):
    if not (type(scope) is InventoryScope and _text_fields(scope.owner,scope.book_uid,scope.book_label,scope.inventory_uid)
            and (scope.branch is None or (type(scope.branch) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,40}",scope.branch)))
            and type(prior_pin) is SnapshotPin and _hex(prior_pin.analysis_id,32) and _hex(prior_pin.revision)):
        raise ContinuityUnavailable
    try:
        if (snapshot.get("analysis_id"),snapshot.get("revision"),
            snapshot["context"].get("owner_external_id")) != (prior_pin.analysis_id,prior_pin.revision,scope.owner):
            raise ContinuityUnavailable
        validate_snapshot_integrity(snapshot,prior_pin.analysis_id)
        result=snapshot["result"]
        native_scope=result["scope"]
        ids=[obj["object_id"] for obj in result["objects"]]
        if (result["scenario"]!="actual" or snapshot["context"].get("cost_object") not in (None,"")
                or type(native_scope) is not dict or set(native_scope)!={"type","object_ids"}
                or native_scope["type"]!="dataset" or type(native_scope["object_ids"]) is not list
                or not ids or not all(safe_text(v,512) for v in ids)
                or sorted(set(ids))!=native_scope["object_ids"] or len(ids)!=len(set(ids))
                or result["ledger_reconciled"] is not True):
            raise ContinuityUnavailable
        old_period=result["period"]
        if _month_idx(old_period)+1!=_month_idx(current_period):
            raise ContinuityUnavailable
        cutoff=_end_before(current_period)
        rec=result["recognition"]
        if scope.branch is not None:
            if rec["operation"]!="raw_composite":
                raise ContinuityUnavailable
            rec=rec["inputs"][scope.branch]
        if rec["operation"]!="moving_weighted_average":
            raise ContinuityUnavailable
        inv=rec["inventory_valuation"]
        exported=inv.get("streams",[inv])
        if type(exported) is not list or not 0<len(exported)<=64:
            raise ContinuityUnavailable
        projected=[]
        source_history=set()
        for stream in exported:
            st=stream["next_state"]
            if (st["book"],st["currency"],st["as_of"])!=(scope.book_label,"CNY",cutoff):
                raise ContinuityUnavailable
            if (stream["context"]["period"],stream["context"]["cutoff_at"])!=(old_period,cutoff):
                raise ContinuityUnavailable
            out=stream["outputs"]
            if (out["closing_quantity"],out["closing_value"])!=(st["quantity"],st["value"]):
                raise ContinuityUnavailable
            ids=st["seen_source_ids"]
            if (type(ids) is not list or len(ids)>MAX_HISTORY or not all(safe_text(x,512) for x in ids)):
                raise ContinuityUnavailable
            normalized_ids={_source_identity(value) for value in ids}
            if len(normalized_ids)!=len(ids) or source_history.intersection(normalized_ids):
                raise ContinuityUnavailable
            source_history.update(normalized_ids)
            projected.append(WarehouseBalance(st["warehouse"],st["item"],st["uom"],st["quantity"],st["value"],_sha(st)))
        closed=inv.get("transfers",{}).get("next_transfer_history",[])
        if type(closed) is not list or len(closed)>MAX_HISTORY:
            raise ContinuityUnavailable
        previous_transfers={transfer_key(x) for x in closed}
        if len(previous_transfers)!=len(closed):
            raise ContinuityUnavailable
        shaped, keys=_warehouse_population(tuple(projected))
        return tuple(projected), shaped, keys, source_history,previous_transfers,cutoff
    except (AttributeError,KeyError,TypeError,ValueError,ArithmeticError,IndexError):
        raise ContinuityUnavailable from None


def native_warehouse_close(*, snapshot, prior_pin, scope, current_period):
    """Project native close only; this operation is NOT a statement of authority."""
    projected, shaped, *_ = _native_boundary(snapshot=snapshot,prior_pin=prior_pin,scope=scope,current_period=current_period)
    return projected,_sha(shaped)


def _transit_population(positions, available, prior_cutoff, previous_transfers, old_sources):
    _need(type(positions) is tuple and len(positions)<=MAX_HISTORY,"invalid transit population")
    rows={}
    source=set()
    for p in positions:
        _need(type(p) is TransitPosition and _text_fields(
            p.from_warehouse,p.to_warehouse,p.item,p.unit,p.dispatch_source_id)
            and _hex(p.dispatch_evidence_sha256),"malformed prior dispatch reference")
        key=transfer_key(p.transfer_id)
        origin=(p.from_warehouse,p.item,p.unit); dest=(p.to_warehouse,p.item,p.unit)
        _need(origin in available and dest in available and origin!=dest,"incompatible dispatch warehouse/item/unit")
        _need(instant(p.dispatch_at)<=instant(prior_cutoff),"dispatch after prior cutoff")
        source_key=_source_identity(p.dispatch_source_id)
        _need(key not in rows and key not in previous_transfers and source_key not in source
              and source_key not in old_sources,
              "duplicate or previously closed transit identity")
        source.add(source_key)
        rows[key]={**asdict(p),"quantity":quantity_text(_qty(p.quantity)),"carrying_value":money_text(_amount(p.carrying_value))}
    return rows,source


def _receipts_population(receipts, transit, current_period, source_history):
    _need(type(receipts) is tuple and len(receipts)<=MAX_HISTORY,"invalid arrival population")
    rows={}
    chronology=set()
    sources=set()
    for r in receipts:
        _need(type(r) is CarriedReceipt and _text_fields(r.warehouse,r.item,r.unit,r.receipt_source_id,r.dispatch_source_id)
              and _hex(r.receipt_evidence_sha256),"malformed arrival evidence")
        key=transfer_key(r.transfer_id)
        bill=transit.get(key)
        _need(bill is not None and key not in rows,"arrival lacks a unique carried dispatch")
        _need(r.transfer_id==bill["transfer_id"] and r.dispatch_source_id==bill["dispatch_source_id"]
              and (r.warehouse,r.item,r.unit)==(bill["to_warehouse"],bill["item"],bill["unit"])
              and _qty(r.quantity)==_qty(bill["quantity"]),"arrival does not match carried source, warehouse, item, unit or qty")
        _need(instant(r.received_at).strftime("%Y-%m")==current_period and
              instant(r.received_at)>instant(bill["dispatch_at"]),"arrival outside the new period")
        _need(type(r.sequence) is int and 0<=r.sequence<=1_000_000 and
              (r.received_at,r.sequence) not in chronology,"ambiguous arrival chronology")
        chronology.add((r.received_at,r.sequence))
        source_key=_source_identity(r.receipt_source_id)
        _need(source_key not in source_history and source_key not in sources,
              "replayed or duplicate receipt source")
        sources.add(source_key)
        rows[key]={**asdict(r),"quantity":quantity_text(_qty(r.quantity))}
    return rows


def preview_rollforward(*, snapshot, prior_pin, scope, current_period,
                        declared_warehouse, declared_transit, reported_receipts,
                        authority: ContinuityAuthority | None):
    """Read-only boundary/receipt validation; neither a next close nor a posting."""
    if authority is None:
        raise ContinuityUnavailable
    _,expected_w,keys,old_sources,closed_ids,cutoff=_native_boundary(
        snapshot=snapshot,prior_pin=prior_pin,scope=scope,current_period=current_period
    )
    try:
        b=authority.read_continuity(scope=scope,prior_pin=prior_pin,current_period=current_period)
        if not (type(b) is ContinuityBinding and b.scope==scope and b.prior_pin==prior_pin
                and b.current_period==current_period and safe_text(b.accepted_close_token,256)
                and all(_hex(v) for v in (b.policy_sha256,b.warehouse_close_sha256,
                                          b.boundary_attestation_sha256,b.arrival_attestation_sha256))
                and b.warehouse_close_sha256==_sha(expected_w)):
            raise ContinuityUnavailable
        t, t_sources=_transit_population(b.transit,keys,cutoff,closed_ids,old_sources)
        receipts=_receipts_population(b.receipts,t,current_period,old_sources|t_sources)
    except Exception:
        raise ContinuityUnavailable from None

    try:
        submitted_w,_=_warehouse_population(declared_warehouse)
        _need(submitted_w==expected_w,"opening warehouse does not match underlying accepted balances")
        submitted_t,_=_transit_population(declared_transit,keys,cutoff,closed_ids,old_sources)
        _need(submitted_t==t,"opening transit does not match the independently attested exact positions")
        submitted_r=_receipts_population(reported_receipts,t,current_period,old_sources|t_sources)
        _need(submitted_r==receipts,"arrivals differ from independently attested exact source records")
    except (ValueError,TypeError,ArithmeticError) as exc:
        if isinstance(exc, ContinuityMismatch): raise
        raise ContinuityMismatch("invalid opening or arrival data") from None

    ordered=sorted(receipts,key=lambda key:(receipts[key]["received_at"],receipts[key]["sequence"]))
    transfers=[{"reported_receipt":receipts[k],"original_dispatch":t[k],
                "asset_reclassification_value":t[k]["carrying_value"],"expense_added":"0.00"} for k in ordered]
    carry=[t[k] for k in sorted(set(t)-set(receipts))]
    # Precondition is deterministic but not a token accepted for write.
    precondition=_sha({"version":1,"scope":asdict(scope),"prior_pin":asdict(prior_pin),"prior_cutoff":cutoff,
                       "period":current_period,"authority_revision":b.accepted_close_token,
                       "policy":b.policy_sha256,"balances":b.warehouse_close_sha256,
                       "boundary":b.boundary_attestation_sha256,"arrivals":b.arrival_attestation_sha256,
                       "bound_transit_population":_sha([t[key] for key in sorted(t)]),
                       "bound_arrival_population":_sha([receipts[key] for key in sorted(receipts)])})
    proposal={"version":1,"status":"matched_read_only_preview","currency":"CNY","period":current_period,
              "opening_warehouse":expected_w,"opening_transit":[t[key] for key in sorted(t)],
              "arrivals_to_stage":transfers,"prior_transit_remaining_after_preview":carry,
              "prior_pin":asdict(prior_pin),"precondition_sha256":precondition,
              "can_claim_complete":False,"posts_entries":False,"publishes_close":False,
              "expense_created_by_preview":"0.00"}
    return {**proposal,"proposal_sha256":_sha(proposal)}
