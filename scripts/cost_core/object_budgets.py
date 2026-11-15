"""Typed comparison against declared, membership-pinned CNY budgets."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
import re

from .models import SourceRef
from .object_rollups import MAX_GROUPS, safe_text


_CENTS = re.compile(r"(?:0|[1-9][0-9]{0,11})\.[0-9]{2}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")


@dataclass(frozen=True, slots=True)
class DeclaredObjectBudget:
    group_id: str
    amount: Decimal
    status: str
    membership_sha256: str
    source_id: str
    source_ref: SourceRef


@dataclass(frozen=True, slots=True)
class ObjectBudgetPlan:
    book: str
    period: str
    currency: str
    rows: tuple[DeclaredObjectBudget, ...]


def need(ok, reason):
    if not ok:
        raise ValueError("composite object budgets: " + reason)


def membership_sha256(scope, group_id, members):
    """Order-insensitive basis pin; labels and costs are not part of membership."""
    policy = {
        "ledger_scope": dict(scope),
        "group_id": group_id,
        "members": sorted([m["input_name"], m["object_id"]] for m in members),
    }
    payload = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _plan_rows(declaration, scope, rollups):
    need(type(rollups) is list and rollups, "explicit object rollups required")
    need(type(declaration) is dict and set(declaration) == {"version", "book", "period", "currency", "rows"}
         and type(declaration["version"]) is int and declaration["version"] == 1,
         "invalid budget declaration fields or version")
    need(safe_text(scope["book"], 128) and _PERIOD.fullmatch(scope["period"]) and
         not scope["period"].startswith("0000") and scope["currency"] == "CNY",
         "invalid CNY ledger scope")
    need(all(declaration[key] == scope[key] for key in ("book", "period", "currency")),
         "budget belongs to different book/period/currency")
    rows = declaration["rows"]
    need(type(rows) is list and 1 <= len(rows) <= MAX_GROUPS, "invalid budget row count")
    bases = {group["id"]: membership_sha256(scope, group["id"], group["members"]) for group in rollups}
    seen = set()
    for index, row in enumerate(rows):
        need(type(row) is dict and set(row) ==
             {"group_id", "amount", "status", "membership_sha256", "source_id"}, "invalid budget row fields")
        group_id, pin, amount = row["group_id"], row["membership_sha256"], row["amount"]
        need(type(group_id) is str and group_id in bases and group_id not in seen,
             "unknown or duplicate budget group")
        seen.add(group_id)
        need(type(pin) is str and _DIGEST.fullmatch(pin) and pin == bases[group_id],
             "budget membership differs from finalized scope")
        need(safe_text(row["source_id"], 160), "invalid budget source identifier")
        need(type(amount) is str and _CENTS.fullmatch(amount),
             "budget must be a bounded non-negative exact CNY cents string")
        need((amount == "0.00" and row["status"] == "evidenced_zero") or
             (amount != "0.00" and row["status"] == "budgeted"), "budget zero status mismatch")
        yield index, row, Decimal(amount)


def validate(declaration, scope, rollups):
    for _ in _plan_rows(declaration, scope, rollups):
        pass


def compile_plan(declaration, scope, rollups, dataset_id, schema_uri):
    rows = tuple(DeclaredObjectBudget(
        group_id=row["group_id"], amount=amount, status=row["status"],
        membership_sha256=row["membership_sha256"], source_id=row["source_id"],
        source_ref=SourceRef(dataset_id=dataset_id, source_uri=schema_uri,
                             selector=f"raw_composite.object_budgets.rows[{index}]",
                             field="object_budgets", kind="configuration"),
    ) for index, row, amount in _plan_rows(declaration, scope, rollups))
    return ObjectBudgetPlan(scope["book"], scope["period"], scope["currency"], rows)


def _money(amount):
    if amount == 0:
        return "0.00"
    left, _, right = format(amount, "f").partition(".")
    return left + "." + right.ljust(2, "0")


def summarize(plan, rollup):
    """Dataset-only comparison. Do not reclassify costs, fill gaps or mutate actuals."""
    scope = {"book": plan.book, "period": plan.period, "currency": plan.currency}
    configured = {row.group_id: row for row in plan.rows}
    if not configured.keys() <= {group["id"] for group in rollup["groups"]}:
        raise ArithmeticError("composite object budget groups disappeared")
    results = []
    with localcontext() as ctx:
        ctx.prec = 64

        def pct(variance, budget):
            if not budget:
                return None
            return _money((variance * 100 / budget).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))

        matched_actual = matched_budget = unmatched_actual = Decimal(0)
        for group in rollup["groups"]:
            gid = group["id"]
            actual = Decimal(group["total_cost"])
            row = {"group_id": gid, "group_label": group["label"], "actual_cost": _money(actual),
                   "budget_cost": None, "variance": None, "variance_pct": None, "budget_status": "missing",
                   "budget_source_id": None, "membership_sha256": None,
                   "source_refs": [dict(ref) for ref in group["source_refs"]]}
            budget = configured.get(gid)
            if budget is not None:
                if membership_sha256(scope, gid, group["members"]) != budget.membership_sha256:
                    raise ArithmeticError("composite object budget finalized membership changed")
                matched_actual += actual
                matched_budget += budget.amount
                difference = actual - budget.amount
                row.update(budget_cost=_money(budget.amount), variance=_money(difference),
                           variance_pct=pct(difference,budget.amount), budget_status=budget.status,
                           budget_source_id=budget.source_id, membership_sha256=budget.membership_sha256)
                row["source_refs"].append(asdict(budget.source_ref))
            else:
                unmatched_actual += actual
            results.append(row)
        ledger = Decimal(rollup["ledger_total"])
        ungrouped = Decimal(rollup["ungrouped_total"])
        if matched_actual + unmatched_actual + ungrouped != ledger:
            raise ArithmeticError("composite object budget comparable and unpaired actual do not reconcile")
        diff = matched_actual - matched_budget
        all_groups = len(configured) == len(results)
        return {"scope": scope, "rows": results, "actual_basis": "finalized_post_allocation_group_amount",
                "plan_basis": "declared_only_not_independently_verified", "can_claim_complete": False,
                "comparison_status": "all_rollup_groups_budgeted" if all_groups else "partial_comparable_group_set",
                "summary": {"matched_group_count": len(configured), "total_group_count": len(results),
                            "comparable_actual": _money(matched_actual), "comparable_budget": _money(matched_budget),
                            "comparable_variance": _money(diff), "comparable_variance_pct": pct(diff,matched_budget),
                            "unmatched_group_actual": _money(unmatched_actual),
                            "ungrouped_ledger_actual": _money(ungrouped), "ledger_actual": _money(ledger)}}
