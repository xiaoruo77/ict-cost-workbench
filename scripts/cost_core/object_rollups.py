"""Explicit cross-branch reporting groups; never generates or reassigns postings."""
import re
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Mapping

from .models import SourceRef


MAX_GROUPS = 96
MAX_MEMBERS_PER_GROUP = 64
MAX_MEMBERS_TOTAL = 512


@dataclass(frozen=True, slots=True)
class RollupMember:
    input_name: str
    object_id: str


@dataclass(frozen=True, slots=True)
class RollupGroup:
    id: str
    label: str
    members: tuple[RollupMember, ...]
    policy_ref: SourceRef


@dataclass(frozen=True, slots=True)
class ObjectRollupPlan:
    groups: tuple[RollupGroup, ...]
    owners: Mapping[str, str]
    pool_owners: frozenset[str]


def require(ok, reason):
    if not ok:
        raise ValueError("composite object rollups: " + reason)


def safe_text(value, limit):
    return (type(value) is str and 0 < len(value) <= limit and value == value.strip()
            and not any(unicodedata.category(c).startswith("C") for c in value))


def validate(spec, branch_names):
    require(type(spec) is list and 0 < len(spec) <= MAX_GROUPS, "invalid group count")
    ids = set()
    used = set()
    for group in spec:
        require(type(group) is dict and set(group) == {"id", "label", "members"}, "invalid group fields")
        ident, label, members = (group[key] for key in ("id", "label", "members"))
        require(type(ident) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", ident),
                "invalid group ID")
        require(ident.casefold() not in ids, "duplicate group ID")
        ids.add(ident.casefold())
        require(safe_text(label, 128), "invalid group label")
        require(type(members) is list and 0 < len(members) <= MAX_MEMBERS_PER_GROUP,
                "invalid group member count")
        for item in members:
            require(type(item) is dict and set(item) == {"input_name", "object_id"}, "invalid member fields")
            branch, obj = item["input_name"], item["object_id"]
            require(type(branch) is str and branch in branch_names, "unknown member branch")
            require(safe_text(obj, 128), "invalid member object ID")
            key = branch, obj
            require(key not in used, "member overlaps groups or is repeated")
            used.add(key)
            require(len(used) <= MAX_MEMBERS_TOTAL, "aggregate rollup membership exceeds limit")


def compile_plan(spec, owners, pool_owners, dataset_id, schema_uri):
    groups = []
    for i, group in enumerate(spec):
        members = group["members"]
        for item in members:
            obj, branch = item["object_id"], item["input_name"]
            require(obj in owners and owners[obj] == branch, "unknown member object or incorrect branch")
            require(obj not in pool_owners, "pool owners cannot be report group members")
        policy_ref=SourceRef(dataset_id=dataset_id, source_uri=schema_uri, field="object_rollups",
                             selector=f"raw_composite.object_rollups[{i}]", kind="configuration")
        groups.append(RollupGroup(group["id"], group["label"],
                      tuple(RollupMember(item["input_name"],item["object_id"]) for item in members), policy_ref))
    return ObjectRollupPlan(tuple(groups), MappingProxyType(dict(owners)), frozenset(pool_owners))


def summarize(plan, rows, ledger_total):
    """Only materialize from final reconciled dataset rows; object reports omit this."""
    by_id = {row["object_id"]: row for row in rows}
    grouped = set()
    groups = []
    with localcontext() as ctx:
        ctx.prec = 60
        for cfg in plan.groups:
            members, refs = [], []
            for member in cfg.members:
                obj = member.object_id
                row = by_id[obj]
                grouped.add(obj)
                members.append({"input_name":member.input_name, "object_id":obj, "total_cost":row["total_cost"]})
                refs.extend(row["source_refs"])
            refs = [asdict(cfg.policy_ref), *refs]
            refs = list({tuple(sorted(ref.items())): ref for ref in refs}.values())
            cost = sum((Decimal(row["total_cost"]) for row in members), Decimal(0))
            groups.append({"id":cfg.id, "label":cfg.label, "members":members,
                           "total_cost":format(cost, "f"), "source_refs":refs})
        ungrouped = [
            {"input_name": plan.owners[row["object_id"]], "object_id": row["object_id"],
             "total_cost": row["total_cost"], "reason": ("pool_owner" if row["object_id"] in plan.pool_owners
                                                          else "no_group_declared")}
            for row in rows if row["object_id"] not in grouped
        ]
        group_total = sum((Decimal(x["total_cost"]) for x in groups), Decimal(0))
        ungrouped_total = sum((Decimal(x["total_cost"]) for x in ungrouped), Decimal(0))
        if group_total + ungrouped_total != ledger_total:
            raise ArithmeticError("composite object rollups do not reconcile with final ledger")
        partial = any(row["reason"] == "no_group_declared" or Decimal(row["total_cost"]) != 0
                      for row in ungrouped)
    return {"groups": groups, "ungrouped": ungrouped,
            "grouped_total": format(group_total, "f"), "ungrouped_total": format(ungrouped_total, "f"),
            "ledger_total": format(ledger_total, "f"),
            "status": "partial" if partial else "all_final_objects_grouped"}
