"""The composition-facing pharmaceutical plugin; filesystem rules stay here."""
from __future__ import annotations
import hashlib
from dataclasses import replace
from pathlib import Path
from cost_core.registry import PluginMetadata, DatasetDefinition, AnalysisRequest
from .adapter import PharmaAdapter, default_context
from .policy import (PLUGIN_ID, PLUGIN_NAME, PLUGIN_VERSION, DEFAULT_DATASET_ID,
                     SELF_ENTITY, PEER_ENTITY, ACTUAL_FILES, BUDGET_FILES, CATEGORY_LABELS,
                     CURRENCY, QUANTITY_UNIT, QUANTITY_LABEL, PRODUCT_SPECS)


class PharmaPlugin:
    metadata = PluginMetadata(plugin_id=PLUGIN_ID, name=PLUGIN_NAME, version=PLUGIN_VERSION,
        capabilities=('monthly_cost', 'period_comparison', 'benchmark', 'weighted_period', 'report_preview'))
    def _adapter(self, dataset, organization=None):
        org = organization or SELF_ENTITY
        if org not in (SELF_ENTITY, PEER_ENTITY): raise ValueError('Unknown organization in this dataset')
        context = replace(default_context(dataset.dataset_id), self_org=org,
                          peer_org=PEER_ENTITY if org == SELF_ENTITY else SELF_ENTITY)
        return PharmaAdapter(context=context, raw_root=Path(dataset.workspace_root)/'raw')
    def describe(self, dataset: DatasetDefinition):
        adapter = self._adapter(dataset)
        rows = adapter.load_observations()
        fingerprint = hashlib.sha256()
        for filename in sorted(set(ACTUAL_FILES.values()) | set(BUDGET_FILES.values())):
            p = Path(dataset.workspace_root)/'raw'/filename
            fingerprint.update(filename.encode());fingerprint.update(p.read_bytes())
        return {'dataset_id':dataset.dataset_id,'plugin_id':dataset.plugin_id,'label':dataset.label,
                'plugin_version':PLUGIN_VERSION,'revision':fingerprint.hexdigest(),
                'currency':CURRENCY,'quantity_unit':QUANTITY_UNIT,'quantity_label':QUANTITY_LABEL,
                'categories':CATEGORY_LABELS,'organizations':sorted({r.organization for r in rows}),
                'cost_objects':sorted({r.cost_object for r in rows}),
                'object_specifications':PRODUCT_SPECS,
                'periods':sorted({r.period for r in rows if r.scenario=='actual'}),
                'report_periods':sorted({r.period for r in rows if r.scenario=='budget'}),
                'observations':len(rows),'default_organization':SELF_ENTITY,
                'report_organizations':[SELF_ENTITY]}
    def load(self, dataset, request):
        adapter=self._adapter(dataset,request.organization)
        rows=adapter.load_observations()  # Strict raw validation, not the legacy injected-row mode.
        if not any(r.cost_object==request.cost_object and r.period==request.period
                   and r.organization==adapter.context.self_org and r.scenario=='actual' for r in rows):
            raise ValueError('No actual record for the requested cost object and period')
        return adapter.load_snapshot(request.cost_object,request.period)
    def preview_report(self,dataset,request):
        if dataset.dataset_id != DEFAULT_DATASET_ID:
            raise ValueError('The legacy pharmaceutical report profile supports its default dataset only')
        if request.organization not in (None,SELF_ENTITY):
            raise ValueError('The current pharmaceutical report profile covers the primary organization only')
        if int(request.period[:4]) not in {year for _org,year in BUDGET_FILES}:
            raise ValueError('No pharmaceutical report mapping for this reference-data year')
        from . import reporting
        expected=Path(reporting.WIKI_ROOT).resolve()
        if Path(dataset.workspace_root).resolve()!=expected:
            raise ValueError('Legacy pharmaceutical report preview is bound to its registered workspace')
        self.load(dataset,request)
        out=reporting.build(request.cost_object,request.period,use_llm=False,verbose=False)
        return {'report':out['report'],'stats':out['stats'],'missing':out['stats']['missing'],
                'profile_id':'pharma_cost_v1','model_calls':0,
                'dataset_id':dataset.dataset_id,'plugin_id':PLUGIN_ID,'cost_object':request.cost_object,
                'period':request.period,'organization':SELF_ENTITY}
