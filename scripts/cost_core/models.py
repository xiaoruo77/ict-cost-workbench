"""Industry-neutral, immutable contracts for deterministic cost analysis."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True, kw_only=True)
class SourceRef:
    dataset_id: str
    source_uri: str
    field: str = ""
    selector: str = ""
    kind: str = "raw"
    formula: str = ""


@dataclass(frozen=True, kw_only=True)
class DatasetContext:
    dataset_id: str
    plugin_id: str
    currency: str
    quantity_unit: str
    self_org: str
    peer_org: str
    category_labels: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "category_labels", MappingProxyType(dict(self.category_labels)))


@dataclass(frozen=True, kw_only=True)
class CostObservation:
    dataset_id: str
    organization: str
    cost_object: str
    period: str
    quantity: Optional[Decimal]
    unit_costs: Mapping[str, Optional[Decimal]]
    reported_unit_cost: Optional[Decimal]
    reported_total: Optional[Decimal]
    currency: str
    quantity_unit: str
    sources: Mapping[str, Tuple[SourceRef, ...]] = field(default_factory=dict)
    scenario: str = "actual"
    source_kind: str = "raw"
    input_periods: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "unit_costs", MappingProxyType(dict(self.unit_costs)))
        object.__setattr__(self, "sources", MappingProxyType(
            {key: tuple(refs) for key, refs in self.sources.items()}))
        object.__setattr__(self, "input_periods", tuple(self.input_periods))


@dataclass(frozen=True, kw_only=True)
class AnalysisInput:
    context: DatasetContext
    cost_object: str
    period: str
    current: CostObservation
    previous: Optional[CostObservation] = None
    year_ago: Optional[CostObservation] = None
    budget: Optional[CostObservation] = None
    peer: Optional[CostObservation] = None
    history: Tuple[CostObservation, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "history", tuple(self.history))


@dataclass(frozen=True, kw_only=True)
class Metric:
    value: Optional[Decimal]
    unit: str
    fact_id: str
    source_refs: Tuple[SourceRef, ...] = ()
    formula: str = ""
    status: str = "observed"


@dataclass(frozen=True, kw_only=True)
class ComparisonRow:
    metric_id: str
    self_value: Optional[Decimal]
    peer_value: Optional[Decimal]
    difference: Optional[Decimal]
    rate: Optional[Decimal]
    direction: str
    unit: str
    source_refs: Tuple[SourceRef, ...] = ()


@dataclass(frozen=True, kw_only=True)
class QualityIssue:
    code: str
    message: str
    metric_id: str = ""
    deviation: Optional[Decimal] = None
    source_refs: Tuple[SourceRef, ...] = ()


@dataclass(frozen=True, kw_only=True)
class AnalysisResult:
    context: DatasetContext
    cost_object: str
    period: str
    metrics: Mapping[str, Metric]
    comparison: Tuple[ComparisonRow, ...] = ()
    history: Tuple[CostObservation, ...] = ()
    quality: Tuple[QualityIssue, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        for name in ("comparison", "history", "quality"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
