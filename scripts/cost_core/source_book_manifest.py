"""Exact source-book coverage verification. No I/O, authentication, or overall cost claims."""
from dataclasses import dataclass
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Iterable

_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.([0-9]{1,6}))?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ROWS = 10000  # same ceiling as the current v2 recognizer

@dataclass(frozen=True)
class Scope:
    workspace: str
    book: str
    currency: str
    period: str
    scope: str      # upstream population definition; not a report label
    version: str    # immutable upstream book snapshot/version, not schema version
    scale: int      # configured currency minor-unit precision

@dataclass(frozen=True)
class Result:
    status: str
    reasons: tuple[str, ...] = ()
    missing_ids: tuple[str, ...] = ()
    extra_ids: tuple[str, ...] = ()
    changed_ids: tuple[str, ...] = ()

def _text(v):
    if not isinstance(v, str) or not v or len(v) > 2048 or v.strip() != v or any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in v):
        raise ValueError("invalid text field")
    return v

def _scope_dict(scope):
    if not isinstance(scope, Scope):
        raise ValueError("expected a scope object")
    for val in (scope.workspace, scope.book, scope.scope, scope.version):
        _text(val)
    if not isinstance(scope.currency,str) or not _CURRENCY.fullmatch(scope.currency):
        raise ValueError("invalid currency")
    if not isinstance(scope.period,str) or not _MONTH.fullmatch(scope.period) or scope.period.startswith("0000"):
        raise ValueError("invalid period")
    if type(scope.scale) is not int or not 0<=scope.scale<=6:
        raise ValueError("invalid currency scale")
    return dict(vars(scope))

def _minor_amount(v, scale):
    if not isinstance(v, str):
        raise ValueError("amount must be an exact decimal string")
    match = _DECIMAL.fullmatch(v)
    if match is None:
        raise ValueError("invalid decimal amount")
    frac = match.group(1) or ""
    if len(frac)>scale:
        raise ValueError("amount exceeds currency precision")
    whole, _, _ = v.removeprefix("-").partition(".")
    return (int(whole)*10**scale + int(frac.ljust(scale,"0") or "0")) * (-1 if v.startswith("-") else 1)

def _digest(obj):
    encoded = json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",",":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _summarize(rows, expected):
    signed = {}
    row_digests = {}
    fields={"id","book","currency","period","kind","amount"}
    for row in rows:
        if len(signed)>=_MAX_ROWS:
            raise ValueError("too many source rows")
        if not isinstance(row, Mapping) or not fields <= set(row) <= fields|{"tags"}:
            raise ValueError("invalid canonical source row shape")
        sid, kind = _text(row["id"]), _text(row["kind"])
        if sid in signed:
            raise ValueError("duplicate source row id")
        if any(row[k] != getattr(expected,k) for k in ("book","currency","period")):
            raise ValueError("source row outside manifest scope")
        tags=row.get("tags",{})
        if not isinstance(tags, Mapping) or len(tags)>24:
            raise ValueError("invalid source tags")
        tags={_text(k):_text(v) for k,v in tags.items()}
        value=_minor_amount(row["amount"], expected.scale)
        signed[sid]=value
        row_digests[sid]=_digest([sid,value,kind,tags])
    ids = sorted(signed)
    positive=sum(v for v in signed.values() if v>0)
    negative=sum(v for v in signed.values() if v<0)
    return dict(count=len(signed),positive_minor=positive,negative_minor=negative,net_minor=positive+negative,
                rows={sid:row_digests[sid] for sid in ids})

def build_manifest(*, rows: Iterable[Mapping], scope: Scope, authority_id: str) -> dict:
    """Construct an upstream export payload; this function does not certify its origin."""
    scope_dict = _scope_dict(scope)
    identity = _text(authority_id)
    body = dict(format_version=1,scope=scope_dict,authority_id=identity,**_summarize(rows, scope))
    return {**body, "digest":_digest(body)}

def compare_manifest(*, manifest, rows: Iterable[Mapping], expected: Scope, trusted_authority_id: str | None = None) -> Result:
    """Caller supplies trusted_authority_id only after actual independent verification.

    The body digest detects changes but is not a signature. Never derive trust
    from uploaded CSV metadata or the submitted manifest fields themselves.
    """
    expected_scope = _scope_dict(expected)
    if manifest is None:
        return Result("not_provided")
    if trusted_authority_id is None:
        return Result("unverified",("independent_authority_not_verified",))
    try:
        identity = _text(trusted_authority_id)
    except ValueError:
        return Result("unverified",("independent_authority_not_verified",))
    if not isinstance(manifest, dict) or manifest.get("authority_id") != identity:
        return Result("unverified",("independent_authority_mismatch",))
    fields={"format_version","scope","authority_id","count","positive_minor","negative_minor","net_minor","rows","digest"}
    if set(manifest)!=fields or type(manifest["format_version"]) is not int or manifest["format_version"]!=1:
        return Result("mismatch",("invalid_manifest_format",))
    authority_scope=manifest["scope"]
    if not isinstance(authority_scope,dict) or type(authority_scope.get("scale")) is not int or authority_scope!=expected_scope:
        return Result("mismatch",("scope_mismatch",))
    claim=manifest["rows"]
    if not isinstance(claim,dict) or len(claim)>_MAX_ROWS or not all(isinstance(v,str) and _DIGEST.fullmatch(v) for v in claim.values()):
        return Result("mismatch",("invalid_manifest_rows",))
    try:
        for k in claim:
            _text(k)
    except ValueError:
        return Result("mismatch",("invalid_manifest_rows",))
    if any(type(manifest[k]) is not int for k in ("count","positive_minor","negative_minor","net_minor")):
        return Result("mismatch",("invalid_manifest_controls",))
    if (manifest["count"]!=len(claim) or manifest["positive_minor"]<0 or manifest["negative_minor"]>0 or
        manifest["net_minor"]!=manifest["positive_minor"]+manifest["negative_minor"] or
        any(abs(manifest[k])>=10**30 for k in ("positive_minor","negative_minor","net_minor"))):
        return Result("mismatch",("invalid_manifest_controls",))
    body={k:manifest[k] for k in fields-{"digest"}}
    if manifest["digest"]!=_digest(body):
        return Result("mismatch",("invalid_manifest_digest",))
    try:
        seen=_summarize(rows,expected)
    except (ValueError,TypeError):
        return Result("mismatch",("invalid_canonical_source_rows",))
    missing = tuple(sorted(set(claim)-set(seen["rows"])))
    extra = tuple(sorted(set(seen["rows"])-set(claim)))
    changed = tuple(sorted(k for k in set(claim)&set(seen["rows"]) if claim[k]!=seen["rows"][k]))
    reasons=tuple(k+"_mismatch" for k in ("count","positive_minor","negative_minor","net_minor") if manifest[k]!=seen[k])
    reasons+=tuple(k for k,items in (("missing_ids",missing),("extra_ids",extra),("changed_ids",changed)) if items)
    return Result("mismatch" if reasons else "matched",reasons,missing,extra,changed)
