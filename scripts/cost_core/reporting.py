"""Industry-neutral report contracts, rendering and execution.

This module performs no model, network, credential or dataset discovery calls.
Industry adapters supply facts, labels, templates and optional narrative work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence


PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")


@dataclass(frozen=True)
class ReportRequest:
    entity_id: str
    period: str
    use_llm: bool = False
    theme: str = "monthly"
    as_of: str | None = None
    dataset_id: str = "default"
    workspace_root: Path | None = None
    today: str | None = None


@dataclass(frozen=True)
class ReportTemplate:
    """An explicit file or inline template; file type is never guessed."""
    text: str | None = None
    path: Path | None = None
    kind: str = "markdown"

    def resolve(self) -> "ReportTemplate":
        if self.path is not None:
            path = Path(self.path)
            if not path.is_file():
                raise FileNotFoundError(f"Report template does not exist: {path}")
            return ReportTemplate(
                text=path.read_text(encoding="utf-8") if self.kind == "markdown" else None,
                path=path, kind=self.kind)
        if self.text is None:
            raise FileNotFoundError("No report template was provided")
        return self


@dataclass(frozen=True)
class ReportProfile:
    profile_id: str
    components: Sequence[str] = ()
    units: Mapping[str, str] = field(default_factory=dict)
    fields: Mapping[str, str] = field(default_factory=dict)
    templates: Mapping[str, ReportTemplate | str | Path] = field(default_factory=dict)
    required_facts: Sequence[str] = ()
    semantic_guards: Sequence[Callable[["ReportContext"], Any]] = ()
    model_budget: Mapping[str, int] = field(default_factory=lambda: {"max_calls": 0})
    missing_data_message: str = "Report data is missing: "
    version: str = "1"


@dataclass
class ReportContext:
    request: ReportRequest
    profile: ReportProfile
    facts: Any
    values: dict[str, Any]
    analysis: dict[str, Any] = field(default_factory=dict)
    stage: str = "prepared"


@dataclass
class RenderedReport:
    report: str
    missing: Sequence[str] = ()
    docx: Path | None = None


def ensure_unique_bindings(pairs: Sequence[tuple[str, str]]) -> None:
    seen: set[str] = set()
    for placeholder, _fact_id in pairs:
        if placeholder in seen:
            raise ValueError(f"Duplicate placeholder binding: {placeholder}")
        seen.add(placeholder)


def render_template(template: str, values: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Fill Unicode placeholders, preserve real zero, and expose missing values."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        value = values.get(name)
        if value is None:
            missing.append(name)
            return "—"
        rendered = str(value)
        if template[match.end():match.end() + 1] == "%" and rendered.endswith("%"):
            rendered = rendered[:-1]
        return rendered

    return re.sub(r"\n{3,}", "\n\n", PLACEHOLDER_RE.sub(replace, template)), missing


def require_complete_facts(facts: Any, required: Sequence[str] = (),
                           message: str = "Report data is missing: ") -> None:
    """Reject declared or required missing data before narrative execution."""
    declared = facts.get("missing", ()) if isinstance(facts, Mapping) else getattr(facts, "missing", ())
    missing = list(declared or ())
    values = facts.get("facts", facts) if isinstance(facts, Mapping) else getattr(facts, "facts", {})
    for name in required:
        value = values.get(name)
        if value is None or getattr(value, "value", value) is None:
            missing.append(name)
    if missing:
        raise ValueError(message + "; ".join(str(item) for item in dict.fromkeys(missing)))


def evaluate_threshold(values: Mapping[str, Any], threshold: float,
                       components: Sequence[str] | None = None,
                       comparator: str = "gte") -> dict[str, Any]:
    """Evaluate any component set, returning explicit unknown and coverage.

    Values can be numeric or fact objects exposing value/display. Invalid or
    non-finite values are unknown, never silently interpreted as zero.
    """
    if comparator not in ("gte", "gt"):
        raise ValueError("Threshold comparator must be 'gte' or 'gt'")
    if not isfinite(float(threshold)) or threshold < 0:
        raise ValueError("Threshold must be a finite nonnegative number")
    labels = list(components if components is not None else values)
    breaches, within, unknown = [], [], []
    for label in labels:
        fact = values.get(label)
        value = getattr(fact, "value", fact)
        try:
            numeric = float(value) if value is not None and not isinstance(value, bool) else float("nan")
        except (TypeError, ValueError):
            numeric = float("nan")
        if not isfinite(numeric):
            unknown.append(label)
            continue
        display = getattr(fact, "display", str(value))
        exceeded = abs(numeric) >= threshold if comparator == "gte" else abs(numeric) > threshold
        (breaches if exceeded else within).append((label, display))
    observed = len(breaches) + len(within)
    coverage = {"expected": len(labels), "observed": observed,
                "fraction": observed / len(labels) if labels else 0.0}
    return {"threshold": threshold, "comparator": comparator, "breaches": breaches,
            "within": within, "unknown": unknown, "coverage": coverage,
            "status": "breached" if breaches else ("unknown" if unknown or not labels else "within")}


class ReportWorkflow:
    """Run preflight, deterministic preparation, optional narration and render.

    Callback signatures:
      prepare_facts(request) -> facts
      prepare_values(facts, request) -> mapping
      narrative(facts, request) -> analysis mapping (called at most once)
      finalize_values(context) -> None (may add profile-specific table fields)
      renderer(template, context) -> RenderedReport (optional, e.g. DOCX)
      stats_builder(context, rendered) -> mapping (optional compatibility view)
    """

    def run(self, request: ReportRequest, profile: ReportProfile, *,
            prepare_facts: Callable[[ReportRequest], Any],
            prepare_values: Callable[[Any, ReportRequest], Mapping[str, Any]],
            narrative: Callable[[Any, ReportRequest], Mapping[str, Any]] | None = None,
            finalize_values: Callable[[ReportContext], Any] | None = None,
            renderer: Callable[[ReportTemplate, ReportContext], RenderedReport] | None = None,
            stats_builder: Callable[[ReportContext, RenderedReport], Mapping[str, Any]] | None = None,
            ) -> dict[str, Any]:
        # Template preflight deliberately precedes every data/model callback.
        source = profile.templates.get(request.theme)
        if source is None:
            raise FileNotFoundError(f"No template for report theme: {request.theme}")
        if isinstance(source, str):
            source = ReportTemplate(text=source)
        elif isinstance(source, Path):
            source = ReportTemplate(path=source)
        template = source.resolve()
        max_calls = int(profile.model_budget.get("max_calls", 0))
        if request.use_llm and (max_calls < 1 or narrative is None):
            raise ValueError("Narrative execution is not permitted by this report profile")
        facts = prepare_facts(request)
        require_complete_facts(facts, profile.required_facts, profile.missing_data_message)
        context = ReportContext(request, profile, facts, dict(prepare_values(facts, request)))
        self._guard(context)
        if request.use_llm:
            context.analysis = dict(narrative(facts, request))
            context.analysis.setdefault("_calls", 1)
            if int(context.analysis.get("_calls", 1)) > max_calls:
                raise ValueError("Narrative exceeded the report model-call budget")
            if any(not isinstance(context.analysis.get(key), str) or not context.analysis[key].strip()
                   for key in profile.fields):
                raise ValueError("Narrative is missing a required profile field")
            character_limit = profile.model_budget.get("max_output_chars")
            if character_limit is not None and sum(
                    len(value) for key, value in context.analysis.items()
                    if not key.startswith("_") and isinstance(value, str)) > character_limit:
                raise ValueError("Narrative exceeded the report character budget")
            context.values.update({key: value for key, value in context.analysis.items()
                                   if not key.startswith("_")})
        if finalize_values is not None:
            finalize_values(context)
        context.stage = "ready_to_render"
        self._guard(context)
        if renderer is None:
            if template.kind != "markdown" or template.text is None:
                raise ValueError("A renderer is required for this template kind")
            report, missing = render_template(template.text, context.values)
            rendered = RenderedReport(report, missing)
        else:
            rendered = renderer(template, context)
        stats = dict(stats_builder(context, rendered)) if stats_builder else {
            "entity_id": request.entity_id, "period": request.period,
            "profile_id": profile.profile_id, "profile_version": profile.version,
            "missing": sorted(set(rendered.missing)),
            "llm_calls": context.analysis.get("_calls", 0),
        }
        return {"report": rendered.report, "facts": facts, "stats": stats,
                "analysis": context.analysis, "docx": rendered.docx}

    @staticmethod
    def _guard(context: ReportContext) -> None:
        for guard in context.profile.semantic_guards:
            if guard(context) is False:
                raise ValueError("Report semantic guard rejected the prepared content")
