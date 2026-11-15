"""Pure monthly comparison of already authorized, immutable actual snapshots.

No route, loader, account lookup, or user-supplied policy authority is defined
here. Only the trusted caller binds the expected workspace owner, book and an
accounting-policy digest to each pinned original report.
"""
import re
from dataclasses import dataclass
from decimal import Decimal, localcontext, ROUND_HALF_UP

from costing_outputs import validate_snapshot_integrity, _source_ids
from cost_core.object_budgets import membership_sha256
from cost_core.object_rollups import safe_text, MAX_GROUPS, MAX_MEMBERS_TOTAL, MAX_MEMBERS_PER_GROUP

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MONTH = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")
_AMOUNT_PLACES = 8  # same bound as the ledger allocation precision
_ATOMS_PER_UNIT = 10 ** _AMOUNT_PLACES
_MONEY = re.compile(rf"-?(0|[1-9][0-9]{{0,29}})(?:\.([0-9]{{1,{_AMOUNT_PLACES}}}))?")
_GROUP = re.compile(r"[A-Za-z0-9_-]{1,40}")


@dataclass(frozen=True, slots=True)
class MonthlyRevision:
    analysis_id: str
    revision: str
    policy_sha256: str


class MonthlyComparisonError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(ok, code):
    if not ok:
        raise MonthlyComparisonError(code)


def month_index(value):
    m = _MONTH.fullmatch(value) if type(value) is str else None
    require(m is not None and int(m[1]) > 0, "invalid_calendar_month")
    return int(m[1]) * 12 + int(m[2])


def amount_atoms(value):
    """Parse integer accounting atoms; never quantize ledger amounts to cents."""
    m = _MONEY.fullmatch(value) if type(value) is str else None
    require(m is not None, "invalid_ledger_money")
    n = int(m[1]) * _ATOMS_PER_UNIT + int((m[2] or "").ljust(_AMOUNT_PLACES, "0"))
    return -n if value.startswith("-") else n


def money(atoms):
    whole, fraction = divmod(abs(atoms), _ATOMS_PER_UNIT)
    digits = f"{fraction:0{_AMOUNT_PLACES}d}".rstrip("0").ljust(2, "0")
    return ("-" if atoms < 0 else "") + f"{whole}.{digits}"


def variance(prior, current):
    delta = current - prior
    with localcontext() as ctx:
        ctx.prec = 96
        pct = None if prior <= 0 else (
            Decimal(delta)*100/prior).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    return money(delta), None if pct is None else ("0.00" if not pct else format(pct, ".2f"))


def _protect(snapshot, pinned, owner):
    require(isinstance(pinned, MonthlyRevision) and
            all(type(x) is str for x in (pinned.analysis_id, pinned.revision, pinned.policy_sha256)) and
            _HEX32.fullmatch(pinned.analysis_id) and _HEX64.fullmatch(pinned.revision)
            and _HEX64.fullmatch(pinned.policy_sha256), "invalid_revision_pin")
    # Same outward result for foreign owner, mismatched pin, missing or corrupted
    # snapshot. An eventual route must also deny before returning either side.
    try:
        ctx = snapshot["context"]
        ok = (ctx.get("owner_external_id") == owner and snapshot.get("revision") == pinned.revision)
        require(ok, "snapshot_unavailable")
        validate_snapshot_integrity(snapshot, pinned.analysis_id)
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError, IndexError):
        raise MonthlyComparisonError("snapshot_unavailable") from None


def _materialize(snapshot, expected_book):
    ctx, result = snapshot["context"], snapshot["result"]
    rec = result.get("recognition", {})
    scope = rec.get("context", {})
    view = rec.get("object_rollups")
    require(ctx.get("scenario") == result.get("scenario") == "actual" and
            not ctx.get("cost_object") and
            result.get("ledger_reconciled") is True and
            rec.get("operation") == "raw_composite" and rec.get("composite_version") == 3 and
            view is not None, "requires_full_canonical_actuals")
    require(ctx.get("period") == result.get("period") == scope.get("period") and
            ctx.get("dataset_id") == result.get("dataset_id") and
            scope.get("book") == expected_book and scope.get("currency") == result.get("currency") == "CNY",
            "incomparable_ledger_scope")
    idx = month_index(scope["period"])
    evidence_ids = {row["evidence_id"] for row in snapshot["evidence"]}

    def refs(row):
        ids = _source_ids(snapshot, row)
        require(set(ids) <= evidence_ids, "incomplete_snapshot_evidence")
        return ids

    object_rows = result["objects"]
    objects = {row["object_id"]: row for row in object_rows}
    require(len(objects) == len(object_rows), "invalid_rollup_partition")
    ledger = amount_atoms(result["totals"]["total_cost"])
    groups, seen_gids, seen_members = {}, set(), set()
    require(0 < len(view["groups"]) <= MAX_GROUPS, "invalid_rollup_partition")
    for g in view["groups"]:
        gid = g["id"]
        require(type(gid) is str and _GROUP.fullmatch(gid) and gid.casefold() not in seen_gids and
                0 < len(g["members"]) <= MAX_MEMBERS_PER_GROUP, "invalid_rollup_partition")
        seen_gids.add(gid.casefold())
        canonical = []
        total = 0
        for m in g["members"]:
            name, obj = m["input_name"], m["object_id"]
            require(safe_text(name, 128) and safe_text(obj, 128) and obj in objects and
                    obj not in seen_members, "invalid_rollup_partition")
            seen_members.add(obj)
            canonical.append((name, obj))
            amount = amount_atoms(m["total_cost"])
            require(amount == amount_atoms(objects[obj]["total_cost"]), "invalid_rollup_partition")
            total += amount
        require(total == amount_atoms(g["total_cost"]) and len(seen_members) <= MAX_MEMBERS_TOTAL,
                "invalid_rollup_partition")
        require(membership_sha256(scope, gid, g["members"]) == g.get("membership_sha256"),
                "invalid_membership_receipt")
        proof = refs(g)
        require(bool(proof), "incomplete_snapshot_evidence")
        groups[gid] = (tuple(sorted(canonical)), total,
                       {"label":g["label"], "amount":money(total), "evidence_ids":proof,
                        "membership_sha256":g["membership_sha256"]})
    ungrouped_total, ungrouped = 0, []
    for row in view["ungrouped"]:
        obj = row["object_id"]
        require(obj in objects and obj not in seen_members and
                row["reason"] in {"no_group_declared", "pool_owner"}, "invalid_rollup_partition")
        seen_members.add(obj)
        amount = amount_atoms(row["total_cost"])
        require(amount == amount_atoms(objects[obj]["total_cost"]), "invalid_rollup_partition")
        ungrouped_total += amount
        ungrouped.append({"input_name":row["input_name"], "object_id":obj, "reason":row["reason"],
                          "amount":money(amount), "evidence_ids":refs(objects[obj])})
    grouped = sum(data[1] for data in groups.values())
    require(seen_members == objects.keys() and
            (grouped, ungrouped_total, ledger) ==
            tuple(amount_atoms(view[k]) for k in ("grouped_total","ungrouped_total","ledger_total")) and
            grouped + ungrouped_total == ledger, "invalid_rollup_partition")
    return idx, groups, ungrouped_total, ungrouped, ledger


def compare_canonical_months(prior_snapshot, current_snapshot, *,
                             expected_workspace_owner, expected_declared_book,
                             prior_pin: MonthlyRevision, current_pin: MonthlyRevision):
    """Compare two full canonical CNY actuals; the authenticated server pins both.

    The policy digest is an independent caller assertion for each revision; no
    uploaded policy/owner fields can authorize use. Merely equal declared book
    does not attest a general ledger. Output never contains tenant identifiers.
    """
    require(safe_text(expected_workspace_owner, 256) and safe_text(expected_declared_book, 128),
            "invalid_trusted_scope")
    _protect(prior_snapshot, prior_pin, expected_workspace_owner)
    _protect(current_snapshot, current_pin, expected_workspace_owner)
    require(prior_pin.policy_sha256 == current_pin.policy_sha256, "incomparable_accounting_policy")
    prev = _materialize(prior_snapshot, expected_declared_book)
    curr = _materialize(current_snapshot, expected_declared_book)
    require(curr[0] - prev[0] == 1, "months_not_adjacent")
    p_groups, c_groups = prev[1], curr[1]
    rows, p_match, c_match, p_unmatched, c_unmatched, nmatch = [], 0, 0, 0, 0, 0
    for gid in sorted(p_groups.keys() | c_groups.keys()):
        p, c = p_groups.get(gid), c_groups.get(gid)
        status = ("added" if p is None else "removed" if c is None
                  else "membership_changed" if p[0] != c[0] else "comparable")
        delta, pct = None, None
        if status == "comparable":
            nmatch += 1
            p_match += p[1]
            c_match += c[1]
            delta, pct = variance(p[1], c[1])
        else:
            p_unmatched += p[1] if p else 0
            c_unmatched += c[1] if c else 0
        rows.append({"group_id":gid, "status":status, "prior":p[2] if p else None,
                     "current":c[2] if c else None, "variance":delta, "variance_pct":pct})
    require(p_match + p_unmatched + prev[2] == prev[4] and
            c_match + c_unmatched + curr[2] == curr[4], "invalid_comparison_reconciliation")
    delta, pct = variance(p_match,c_match) if nmatch else (None,None)
    def side(snapshot, matched, unmatched, data):
        return {"analysis_id":snapshot["analysis_id"], "revision":snapshot["revision"],
                "period":snapshot["context"]["period"], "comparable_actual":money(matched),
                "unmatched_actual":money(unmatched), "ungrouped_actual":money(data[2]),
                "ledger_actual":money(data[4]), "ungrouped":data[3],
                "source_recognition_status":snapshot["result"]["recognition"].get("source_recognition_status")}
    return {"schema_version":"costing-month-comparison/1", "currency":"CNY",
            "book_basis":"same_declared_book_not_independently_attested",
            "policy_sha256":prior_pin.policy_sha256, "can_claim_complete":False,
            "prior":side(prior_snapshot,p_match,p_unmatched,prev),
            "current":side(current_snapshot,c_match,c_unmatched,curr), "groups":rows,
            "comparable_group_count":nmatch, "comparable_variance":delta, "comparable_variance_pct":pct}
