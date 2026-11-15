"""Isolated, standard-library cost recognition reference. Not production code.

The JSON spec is data, not a program: four whitelisted rule operations only.
Money is Decimal; source money must already be in currency minor units.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, Context as DecimalContext, ROUND_HALF_UP, localcontext
from enum import Enum
import json
import re
from typing import Any


class Invalid(ValueError):
    pass


MAX_SOURCES = 500
MAX_RULES = 500
MAX_OUTPUTS = 100
MAX_FANOUT = 64
MAX_DEPTH = 64
MAX_REFERENCES = 5000
MAX_DOCUMENT_BYTES = 512 * 1024


class Kind(str, Enum):
    EXPENSE = "expense"
    ACCRUAL = "accrual"
    CREDIT = "credit"
    ASSET = "asset_basis"
    PREPAY = "prepay_basis"
    OPENING = "opening_balance"
    CLOSING = "closing_balance"
    CASH = "cash_movement"


class Status(str, Enum):
    DATA = "data"
    ZERO = "evidenced_zero"
    MISSING = "missing"
    NA = "not_applicable"


class Op(str, Enum):
    RECOGNIZE = "recognize"
    MONTHLY = "monthly_straight_line"
    SUM = "sum"
    BRIDGE = "inventory_bridge"


@dataclass(frozen=True)
class Context:
    book: str
    period: str
    currency: str


@dataclass(frozen=True)
class Source:
    id: str
    book: str
    period: str
    currency: str
    kind: Kind
    status: Status
    amount: Decimal | None
    economic_key: str
    evidence: str
    bridge_group: str | None = None
    origin_period: str | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    op: Op
    source: str | None = None
    inputs: tuple[str, ...] = ()
    start: str | None = None
    months: int | None = None
    flow: str | None = None
    opening: str | None = None
    closing: str | None = None
    posting_pool: str | None = None


@dataclass(frozen=True)
class Spec:
    name: str
    context: Context
    sources: tuple[Source, ...]
    rules: tuple[Rule, ...]
    outputs: tuple[str, ...]
    excluded_sources: dict[str, str]


@dataclass(frozen=True)
class Value:
    amount: Decimal
    # Economic components, not source IDs: aliases cannot evade duplication checks.
    components: frozenset[str]
    source_ids: frozenset[str]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Invalid(message)


def keys(obj: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    check(isinstance(obj, dict), "Expected an object")
    check(required <= obj.keys(), f"Missing fields: {sorted(required - obj.keys())}")
    check(obj.keys() <= required | optional,
          f"Unknown fields: {sorted(obj.keys() - required - optional)}")


def text(value: Any) -> str:
    check(isinstance(value, str) and bool(value.strip()) and len(value) <= 4096,
          "Expected nonempty text of at most 4096 characters")
    return value


def month(value: Any) -> int:
    check(isinstance(value, str) and bool(re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value)),
          "Invalid YYYY-MM period")
    year, mo = map(int, value.split("-"))
    check(year >= 1, "Invalid year")
    return year * 12 + mo - 1


def money(value: Any) -> Decimal:
    check(isinstance(value, str) and bool(re.fullmatch(r"-?[0-9]{1,18}(\.[0-9]{1,2})?", value)),
          "Money must be a finite decimal string with at most two decimal places")
    return Decimal(value)


def seq(value: Any) -> list:
    check(isinstance(value, list), "Expected an array")
    return value


def load(raw: dict) -> Spec:
    """Runtime-typed, strict JSON boundary. Unknown operators/fields fail closed."""
    keys(raw, {"name", "context", "sources", "rules", "outputs", "excluded_sources"})
    ctx = raw["context"]
    keys(ctx, {"book", "period", "currency"})
    month(ctx["period"])
    check(isinstance(ctx["currency"], str) and bool(re.fullmatch("[A-Z]{3}", ctx["currency"])),
          "Invalid currency")
    context = Context(text(ctx["book"]), ctx["period"], ctx["currency"])
    check(len(seq(raw["sources"])) <= MAX_SOURCES, "Too many sources")
    check(len(seq(raw["rules"])) <= MAX_RULES, "Too many rules")
    check(len(seq(raw["outputs"])) <= MAX_OUTPUTS, "Too many outputs")
    sources = []
    for item in seq(raw["sources"]):
        keys(item, {"id", "book", "period", "currency", "kind", "status", "amount",
                    "economic_key", "evidence"}, {"bridge_group", "origin_period"})
        try:
            kind, status = Kind(item["kind"]), Status(item["status"])
        except (ValueError, TypeError) as exc:
            raise Invalid("Unknown source kind/status") from exc
        amount = None if item["amount"] is None else money(item["amount"])
        source = Source(text(item["id"]), text(item["book"]), text(item["period"]),
                        text(item["currency"]), kind, status, amount,
                        text(item["economic_key"]), text(item["evidence"]),
                        text(item["bridge_group"]) if "bridge_group" in item else None,
                        text(item["origin_period"]) if "origin_period" in item else None)
        month(source.period)
        if kind in (Kind.ASSET, Kind.PREPAY):
            check(source.origin_period is not None, "Basis requires original origin_period")
        if source.origin_period is not None:
            check(month(source.origin_period) <= month(source.period),
                  "Origin period cannot be after snapshot period")
        if status in (Status.DATA, Status.ZERO):
            check(amount is not None, f"Missing amount: {source.id}")
            check(status != Status.ZERO or amount == 0, "Evidenced zero must equal zero")
            check(amount <= 0 if kind == Kind.CREDIT else amount >= 0,
                  f"Invalid sign for {kind.value}: {source.id}")
        else:
            check(amount is None, "Missing/not-applicable source must not carry a number")
        sources.append(source)
    rules = []
    for item in seq(raw["rules"]):
        check(isinstance(item, dict), "Expected a rule object")
        try:
            op = Op(item.get("op"))
        except (ValueError, TypeError) as exc:
            raise Invalid("Unsupported rule operation") from exc
        fields = {Op.RECOGNIZE: {"source", "posting_pool"},
                  Op.MONTHLY: {"source", "start", "months", "posting_pool"},
                  Op.SUM: {"inputs"}, Op.BRIDGE: {"flow", "opening", "closing"}}[op]
        keys(item, {"id", "op"} | fields)
        kwargs = {key: item[key] for key in fields}
        if op == Op.SUM:
            kwargs["inputs"] = tuple(text(x) for x in seq(item["inputs"]))
            check(bool(kwargs["inputs"]), "Empty sum is not evidenced zero")
            check(len(kwargs["inputs"]) <= MAX_FANOUT, "Rule fanout limit exceeded")
        else:
            for key in fields - {"months"}:
                text(item[key])
        if op == Op.MONTHLY:
            month(item["start"])
            check(type(item["months"]) is int and 1 <= item["months"] <= 1200,
                  "Schedule months must be an integer from 1 to 1200")
        rules.append(Rule(text(item["id"]), op, **kwargs))
    check(sum(len(rule.inputs) + (1 if rule.flow else 0) for rule in rules) <= MAX_REFERENCES,
          "Reference count limit exceeded")
    outputs = tuple(text(x) for x in seq(raw["outputs"]))
    check(bool(outputs), "No output declared")
    check(len(outputs) == len(set(outputs)), "Duplicate output")
    exclusions = raw["excluded_sources"]
    check(isinstance(exclusions, dict), "Expected exclusion map")
    for key, reason in exclusions.items():
        text(key)
        text(reason)
    return Spec(text(raw["name"]), context, tuple(sources), tuple(rules), outputs, exclusions.copy())


def evaluate(spec: Spec) -> dict:
    """Evaluate all declared rules atomically, never returning partial results."""
    # Do not inherit an embedding application's precision, exponent or rounding settings.
    with localcontext(DecimalContext(prec=60, rounding=ROUND_HALF_UP)):
        return _evaluate(spec)


def _evaluate(spec: Spec) -> dict:
    sources = {x.id: x for x in spec.sources}
    rules = {x.id: x for x in spec.rules}
    check(len(sources) == len(spec.sources), "Duplicate source ID")
    check(len(rules) == len(spec.rules), "Duplicate rule ID")
    check(not (sources.keys() & rules.keys()), "Source and rule IDs must be disjoint")
    check(bool(spec.outputs), "No output declared")
    for source in sources.values():
        check((source.book, source.period, source.currency) ==
              (spec.context.book, spec.context.period, spec.context.currency),
              f"Stale or cross-book/period/currency source: {source.id}")
        check(source.status != Status.MISSING, f"Missing source: {source.id}")
    check(spec.excluded_sources.keys() <= sources.keys(), "Undefined excluded source")
    visited: dict[str, Value] = {}
    depths: dict[str, int] = {}
    active: set[str] = set()
    trace: dict[str, dict] = {}
    used: set[str] = set()

    def get_source(sid: str | None, kinds: set[Kind]) -> Source:
        check(sid in sources, f"Undefined source: {sid}")
        source = sources[sid]
        check(source.status in (Status.DATA, Status.ZERO), f"Unavailable source: {sid}")
        check(source.kind in kinds, f"Invalid source kind for this rule: {sid}")
        check(sid not in spec.excluded_sources, f"Source both consumed and excluded: {sid}")
        used.add(sid)
        return source

    def combine(values: list[Value], where: str) -> tuple[frozenset[str], frozenset[str]]:
        count = Counter(component for val in values for component in val.components)
        duplicates = sorted(key for key, n in count.items() if n > 1)
        check(not duplicates, f"Double recognition at {where}: {duplicates}")
        return (frozenset(count), frozenset(s for val in values for s in val.source_ids))

    def visit(rid: str) -> Value:
        check(rid in rules, f"Undefined rule/output: {rid}")
        check(rid not in active, f"Cycle detected at {rid}")
        if rid in visited:
            return visited[rid]
        check(len(active) < MAX_DEPTH, "Rule dependency depth limit exceeded")
        active.add(rid)
        rule = rules[rid]
        detail: dict = {"op": rule.op.value}
        if rule.op in (Op.RECOGNIZE, Op.MONTHLY):
            kinds = ({Kind.EXPENSE, Kind.ACCRUAL, Kind.CREDIT} if rule.op == Op.RECOGNIZE
                     else {Kind.ASSET, Kind.PREPAY})
            source = get_source(rule.source, kinds)
            amount = source.amount
            detail["source"] = source.id
            if rule.op == Op.MONTHLY:
                check(month(source.origin_period) <= month(rule.start),
                      "Schedule starts before original basis origin period")
                index = month(spec.context.period) - month(rule.start)
                check(0 <= index < rule.months, f"Inactive schedule: {rid}")
                # Integer-minor-unit schedule: residual recognized in final month.
                cents = int(amount * 100)
                base, residual = divmod(cents, rule.months)
                charge = base + (residual if index == rule.months - 1 else 0)
                amount = Decimal(charge) / 100
                detail.update(month_number=index + 1, months=rule.months,
                              origin_period=source.origin_period,
                              coverage_start=rule.start,
                              recognition_period=spec.context.period,
                              basis=format(source.amount, ".2f"),
                              rounding_policy="floor_minor_units_then_final_month_residual",
                              residual_minor_units=residual if index == rule.months - 1 else 0)
            val = Value(amount, frozenset({source.economic_key}), frozenset({source.id}))
        elif rule.op == Op.SUM:
            values = [visit(x) for x in rule.inputs]
            components, ids = combine(values, rid)
            val = Value(sum((x.amount for x in values), Decimal(0)), components, ids)
            detail["inputs"] = list(rule.inputs)
        elif rule.op == Op.BRIDGE:
            flow = visit(rule.flow)
            opening = get_source(rule.opening, {Kind.OPENING})
            closing = get_source(rule.closing, {Kind.CLOSING})
            check(bool(opening.bridge_group) and opening.bridge_group == closing.bridge_group,
                  f"Mismatched balance bridge: {rid}")
            values = [flow,
                      Value(opening.amount, frozenset({opening.economic_key}), frozenset({opening.id})),
                      Value(closing.amount, frozenset({closing.economic_key}), frozenset({closing.id}))]
            components, ids = combine(values, rid)
            amount = opening.amount + flow.amount - closing.amount
            check(flow.amount >= 0 and amount >= 0, f"Negative inventory bridge: {rid}")
            val = Value(amount, components, ids)
            detail.update(flow=rule.flow, opening=opening.id, closing=closing.id,
                          formula="opening + current_flow - closing")
        else:
            raise Invalid("Unsupported rule operation")
        dependencies = rule.inputs if rule.op == Op.SUM else ((rule.flow,) if rule.op == Op.BRIDGE else ())
        depths[rid] = 1 + max((depths[dep] for dep in dependencies), default=0)
        check(depths[rid] <= MAX_DEPTH, "Rule dependency depth limit exceeded")
        detail.update(amount=format(val.amount, ".2f"), source_ids=sorted(val.source_ids),
                      economic_components=sorted(val.components))
        trace[rid] = detail
        active.remove(rid)
        visited[rid] = val
        return val

    # Validate disconnected nodes, too: dead invalid rules must not be hidden.
    for rid in rules:
        visit(rid)
    for rid in spec.outputs:
        visit(rid)
    check(used | spec.excluded_sources.keys() == sources.keys(),
          f"Unclassified source coverage: {sorted(sources.keys() - used - spec.excluded_sources.keys())}")
    check(bool(used), "No recognized sources")
    postings = []
    posted_keys = set()
    pools: dict[str, Decimal] = {}
    for rid, rule in rules.items():
        if rule.op not in (Op.RECOGNIZE, Op.MONTHLY):
            continue
        value = visited[rid]
        check(not (posted_keys & value.components), f"Duplicate posting economic component: {rid}")
        posted_keys.update(value.components)
        pools[rule.posting_pool] = pools.get(rule.posting_pool, Decimal(0)) + value.amount
        postings.append({"recognition_rule": rid, "posting_pool": rule.posting_pool,
                         "amount": format(value.amount, ".2f"),
                         "source_ids": sorted(value.source_ids),
                         "economic_components": sorted(value.components)})
    nonzero_excluded = [{"source_id": sid, "kind": sources[sid].kind.value,
                        "amount": format(sources[sid].amount, ".2f"), "reason": reason}
                       for sid, reason in spec.excluded_sources.items()
                       if sources[sid].kind != Kind.CASH and sources[sid].amount not in (None, Decimal(0))]
    limited = any(sources[sid].kind != Kind.CASH and sources[sid].status != Status.NA
                  for sid in spec.excluded_sources)
    return {"status": "valid", "scope": "synthetic_reference_not_production_acceptance",
            "can_claim_complete": False,
            "coverage_status": "partial_declared_exclusions" if limited else "declared_input_coverage_only",
            "unrecognized_nonzero_sources": nonzero_excluded,
            "model": spec.name, "context": vars(spec.context),
            "posted_costs": postings,
            "posted_pool_totals": {pool: format(value, ".2f") for pool, value in pools.items()},
            "derived_views": {rid: {"amount": format(visited[rid].amount, ".2f"),
                                     "treatment": "non_posting_overlapping_view",
                                     "source_ids": sorted(visited[rid].source_ids)}
                              for rid in spec.outputs if rules[rid].op in (Op.SUM, Op.BRIDGE)},
            "memo_sources": [{"source_id": sid, "kind": sources[sid].kind.value,
                               "amount": (None if sources[sid].amount is None else format(sources[sid].amount, ".2f")),
                               "reason": reason, "treatment": "not_posted"}
                              for sid, reason in spec.excluded_sources.items()],
            "balance_inputs": [{"source_id": s.id, "kind": s.kind.value,
                                 "amount": format(s.amount, ".2f"), "treatment": "bridge_input_not_posted"}
                                for s in spec.sources if s.kind in (Kind.OPENING, Kind.CLOSING) and s.id in used],
            "outputs": {rid: format(visited[rid].amount.quantize(Decimal("0.01"),
                                                                  rounding=ROUND_HALF_UP), ".2f")
                        for rid in spec.outputs},
            "trace": trace, "excluded_sources": spec.excluded_sources,
            "source_coverage": {s.id: {"status": s.status.value, "evidence": s.evidence,
                                        "treatment": "excluded" if s.id in spec.excluded_sources else "consumed"}
                                for s in spec.sources}}


def run_document(raw: dict) -> dict:
    try:
        return evaluate(load(raw))
    except Invalid as exc:
        return {"status": "invalid", "error": str(exc), "outputs": {}}


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as handle:
        data = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        result = {"status": "invalid", "error": "Document byte limit exceeded", "outputs": {}}
    else:
        try:
            result = run_document(json.loads(data))
        except (ValueError, UnicodeError, RecursionError):
            result = {"status": "invalid", "error": "Malformed JSON document", "outputs": {}}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["status"] == "valid" else 1)
