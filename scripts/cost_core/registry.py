"""Trusted plugin composition and dataset-scoped services. No industry imports."""
from __future__ import annotations
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Protocol
import re
from . import engine
from .models import AnalysisInput, AnalysisResult
from .numbers import format_number


class UnknownPluginError(LookupError): pass
class UnknownDatasetError(LookupError): pass


@dataclass(frozen=True, kw_only=True)
class PluginMetadata:
    plugin_id: str
    name: str
    version: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class DatasetDefinition:
    dataset_id: str
    plugin_id: str
    workspace_root: Path
    label: str


@dataclass(frozen=True, kw_only=True)
class AnalysisRequest:
    dataset_id: str
    cost_object: str
    period: str
    organization: str | None = None
    plugin_id: str | None = None


class IndustryPlugin(Protocol):
    metadata: PluginMetadata
    def describe(self, dataset: DatasetDefinition) -> Mapping[str, Any]: ...
    def load(self, dataset: DatasetDefinition, request: AnalysisRequest) -> AnalysisInput: ...
    def preview_report(self, dataset: DatasetDefinition, request: AnalysisRequest) -> Mapping[str, Any]: ...


def to_primitive(value):
    """Serialize frozen mappings without dataclasses.asdict/deepcopy."""
    if isinstance(value, Decimal): return format(value, 'f')
    if isinstance(value, Path): return str(value)
    if is_dataclass(value): return {f.name: to_primitive(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping): return {str(k): to_primitive(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)): return [to_primitive(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)): return value
    raise TypeError(f'Unsupported result value: {type(value).__name__}')


class PluginRegistry:
    def __init__(self): self._plugins = {}
    def register(self, plugin: IndustryPlugin):
        meta = plugin.metadata
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', meta.plugin_id):
            raise ValueError('Invalid trusted plugin identifier')
        if meta.plugin_id in self._plugins: raise ValueError('Duplicate plugin identifier')
        self._plugins[meta.plugin_id] = plugin
    def get(self, plugin_id: str):
        try: return self._plugins[plugin_id]
        except KeyError: raise UnknownPluginError(f'Unknown plugin: {plugin_id}') from None
    def list(self): return tuple(p.metadata for p in self._plugins.values())


class CostService:
    def __init__(self, registry: PluginRegistry, datasets: tuple[DatasetDefinition, ...]):
        self.registry = registry
        self._datasets = {}
        for dataset in datasets:
            if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', dataset.dataset_id):
                raise ValueError('Invalid dataset identifier')
            if dataset.dataset_id in self._datasets: raise ValueError('Duplicate dataset identifier')
            registry.get(dataset.plugin_id)
            self._datasets[dataset.dataset_id] = dataset
    def _resolve(self, request: AnalysisRequest):
        try: dataset = self._datasets[request.dataset_id]
        except KeyError: raise UnknownDatasetError(f'Unknown dataset: {request.dataset_id}') from None
        if request.plugin_id is not None and request.plugin_id != dataset.plugin_id:
            raise UnknownPluginError('Requested plugin does not own this dataset')
        if not request.cost_object or not request.cost_object.strip(): raise ValueError('A cost object is required')
        engine.month_index(request.period)
        return dataset, self.registry.get(dataset.plugin_id)
    def datasets(self):
        return tuple(dict(self.registry.get(d.plugin_id).describe(d)) for d in self._datasets.values())
    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        dataset, plugin = self._resolve(request)
        snapshot = plugin.load(dataset, request)
        if snapshot.context.dataset_id != dataset.dataset_id or snapshot.context.plugin_id != dataset.plugin_id:
            raise ValueError('Plugin returned data belonging to another dataset or plugin')
        if snapshot.period != request.period or snapshot.cost_object != request.cost_object:
            raise ValueError('Plugin returned a different analysis scope')
        if request.organization and snapshot.context.self_org != request.organization:
            raise ValueError('Plugin returned a different organization')
        return engine.analyze(snapshot)
    def analyze_payload(self, request: AnalysisRequest):
        result = self.analyze(request)
        data = to_primitive(result)
        for metric_id, metric in result.metrics.items():
            data['metrics'][metric_id]['display'] = format_number(metric.value)
        for row, source in zip(data['comparison'], result.comparison):
            row['display_difference'] = format_number(source.difference, signed=True)
            row['display_rate'] = (format_number(source.rate, signed=True) + '%'
                                   if source.rate is not None else '—')
        return data
    def preview_report(self, request: AnalysisRequest):
        dataset, plugin = self._resolve(request)
        # The public preview contract has no paid-generation option.
        result = dict(plugin.preview_report(dataset, request))
        expected = {'dataset_id':dataset.dataset_id,'plugin_id':dataset.plugin_id,
                    'cost_object':request.cost_object,'period':request.period}
        if any(result.get(key) != value for key,value in expected.items()):
            raise ValueError('Plugin returned a report from a different analysis scope')
        if request.organization and result.get('organization') != request.organization:
            raise ValueError('Plugin returned a report from a different organization')
        if result.get('model_calls') != 0:
            raise ValueError('Preview unexpectedly used a model')
        result.update(dataset_id=dataset.dataset_id, plugin_id=dataset.plugin_id, dry_run=True, model_calls=0)
        return result
