"""Trusted, code-shipped industry adapters for the common cost ledger.

Industry plugins define source mappings and business metadata only. Allocation,
reconciliation, unit-cost, contribution and variance calculations are delegated
to cost_core.costing.CostingEngine.
"""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from cost_core.models import SourceRef
from cost_core.costing import (
    AllocationRule, CostBenchmark, CostDriver, CostEntry, CostObject, CostPool,
    CostingDatasetDefinition, CostingInput, CostingPluginMetadata,
    CostingInput as LedgerInput, StandardCost,
)
from cost_core.csv_adapter import CsvCostingAdapter


D = Decimal


def _ref(dataset_id: str, uri: str, field: str, selector: str, formula: str = ""):
    return SourceRef(dataset_id=dataset_id, source_uri=uri, field=field,
                    selector=selector, kind="synthetic", formula=formula)


def _scaled(value: str, factor: Decimal) -> Decimal:
    return (D(value) * factor).quantize(D("0.01"))


class RestaurantPlugin:
    metadata = CostingPluginMetadata(
        plugin_id="restaurant", name="餐饮业成本适配器", version="1.0.0",
        industry="餐饮", capabilities=("menu_cost", "food_cost", "labor_allocation",
                                      "rent_allocation", "energy_allocation", "margin"),
    )

    def describe(self, dataset: CostingDatasetDefinition) -> Mapping[str, Any]:
        return {
            "dataset_id": dataset.dataset_id, "plugin_id": self.metadata.plugin_id,
            "label": dataset.label, "version": dataset.version, "industry": "餐饮",
            "currency": "CNY", "quantity_unit": "份",
            "periods": ["2026-05", "2026-06"],
            "scenarios": ["actual", "budget", "standard"],
            "data_kind": "synthetic",
            "scenario_basis": "显式设定的教学样本；actual 是账簿情景名，不代表真实企业实绩；预算和标准使用已声明演示假设",
            "objects": ["招牌鸡腿饭", "番茄牛肉面", "菌菇汤"],
            "cost_objects": [{"object_id": key, "name": name} for key, name in (("dish-rice", "招牌鸡腿饭"), ("dish-noodle", "番茄牛肉面"), ("dish-soup", "菌菇汤"))],
            "description": "模拟门店菜品、食材、人工、租金、能源与平台服务费数据",
        }

    def load(self, dataset: CostingDatasetDefinition, period: str, scenario: str) -> CostingInput:
        if period not in {"2026-05", "2026-06"}:
            raise FileNotFoundError(f"餐饮样例没有 {period} 期间数据")
        if scenario not in {"actual", "budget", "standard"}:
            raise ValueError("餐饮样例支持 actual、budget、standard 情景")
        factor = D("1") if scenario == "actual" else (D("1.04") if scenario == "budget" else D("0.98"))
        dataset_id = dataset.dataset_id
        objects = (
            CostObject(object_id="dish-rice", name="招牌鸡腿饭", kind="menu_item",
                       quantity=D("1200"), quantity_unit="份", currency="CNY", selling_price=D("22.00")),
            CostObject(object_id="dish-noodle", name="番茄牛肉面", kind="menu_item",
                       quantity=D("900"), quantity_unit="份", currency="CNY", selling_price=D("26.00")),
            CostObject(object_id="dish-soup", name="菌菇汤", kind="menu_item",
                       quantity=D("700"), quantity_unit="份", currency="CNY", selling_price=D("16.00")),
            CostObject(object_id="cc-kitchen-labor", name="后厨人工成本中心", kind="cost_center",
                       currency="CNY"),
            CostObject(object_id="cc-rent", name="门店租金成本中心", kind="cost_center",
                       currency="CNY"),
            CostObject(object_id="cc-energy", name="门店能源成本中心", kind="cost_center",
                       currency="CNY"),
            CostObject(object_id="cc-platform", name="平台服务费成本中心", kind="cost_center",
                       currency="CNY"),
        )
        entries = []
        for object_id, element, amount, behavior in (
            ("dish-rice", "food_material", "8400", "variable"),
            ("dish-rice", "packaging", "960", "variable"),
            ("dish-noodle", "food_material", "7650", "variable"),
            ("dish-noodle", "packaging", "720", "variable"),
            ("dish-soup", "food_material", "2800", "variable"),
            ("dish-soup", "packaging", "350", "variable"),
        ):
            entries.append(CostEntry(
                entry_id=f"{object_id}-{element}", object_id=object_id,
                element_id=element, amount=_scaled(amount, factor), currency="CNY",
                cost_type="direct", behavior=behavior,
                source_refs=(_ref(dataset_id, "sample://restaurant/pos-and-purchase",
                                  element, f"period={period}&object={object_id}"),),
            ))
        pools = (
            CostPool(pool_id="pool-kitchen-labor", name="后厨人工", owner_id="cc-kitchen-labor",
                     amount=_scaled("18000", factor), currency="CNY", element_id="labor",
                     behavior="semi_variable", variable_fraction=D("0.35"),
                     source_refs=(_ref(dataset_id, "sample://restaurant/payroll", "labor_total", period),
                                  _ref(dataset_id, "sample://restaurant/assumptions", "variable_fraction", period, "教学假设：人工变动比例 0.35"))),
            CostPool(pool_id="pool-rent", name="门店租金", owner_id="cc-rent",
                     amount=_scaled("12000", factor), currency="CNY", element_id="rent",
                     behavior="fixed",
                     source_refs=(_ref(dataset_id, "sample://restaurant/lease", "rent", period),)),
            CostPool(pool_id="pool-energy", name="水电燃气", owner_id="cc-energy",
                     amount=_scaled("4200", factor), currency="CNY", element_id="energy",
                     behavior="semi_variable", variable_fraction=D("0.70"),
                     source_refs=(_ref(dataset_id, "sample://restaurant/utility", "energy_cost", period),
                                  _ref(dataset_id, "sample://restaurant/assumptions", "variable_fraction", period, "教学假设：能源变动比例 0.70"))),
            CostPool(pool_id="pool-platform", name="外卖平台服务费", owner_id="cc-platform",
                     amount=_scaled("2400", factor), currency="CNY", element_id="platform_fee",
                     behavior="variable",
                     source_refs=(_ref(dataset_id, "sample://restaurant/platform-statement", "fee", period),)),
        )
        drivers = (
            CostDriver(driver_id="labor-hours", name="菜品标准人工工时", unit="小时",
                       values={"dish-rice": D("200"), "dish-noodle": D("160"), "dish-soup": D("90")},
                       source_refs=(_ref(dataset_id, "sample://restaurant/shift-sheet", "hours", period),)),
            CostDriver(driver_id="revenue-share", name="菜品销售收入", unit="CNY",
                       values={"dish-rice": D("26400"), "dish-noodle": D("23400"), "dish-soup": D("11200")},
                       source_refs=(_ref(dataset_id, "sample://restaurant/pos", "revenue", period),)),
            CostDriver(driver_id="energy-kwh", name="设备能耗分摊量", unit="kWh",
                       values={"dish-rice": D("520"), "dish-noodle": D("430"), "dish-soup": D("250")},
                       source_refs=(_ref(dataset_id, "sample://restaurant/meter", "kwh", period),)),
        )
        rules = (
            AllocationRule(rule_id="allocate-kitchen-labor", pool_id="pool-kitchen-labor",
                           target_ids=("dish-rice", "dish-noodle", "dish-soup"),
                           method="driver", driver_id="labor-hours", stage=1,
                           description="按菜品标准人工工时分配后厨人工"),
            AllocationRule(rule_id="allocate-rent", pool_id="pool-rent",
                           target_ids=("dish-rice", "dish-noodle", "dish-soup"),
                           method="driver", driver_id="revenue-share", stage=1,
                           description="按菜品销售收入占比分配租金"),
            AllocationRule(rule_id="allocate-energy", pool_id="pool-energy",
                           target_ids=("dish-rice", "dish-noodle", "dish-soup"),
                           method="driver", driver_id="energy-kwh", stage=1,
                           description="按设备能耗分配水电燃气"),
            AllocationRule(rule_id="allocate-platform", pool_id="pool-platform",
                           target_ids=("dish-rice", "dish-noodle", "dish-soup"),
                           method="driver", driver_id="revenue-share", stage=1,
                           description="按销售收入分配平台服务费"),
        )
        if period == "2026-06":
            quantities = {"dish-rice": D("1260"), "dish-noodle": D("855"), "dish-soup": D("770")}
            ratios = {obj.object_id: quantities[obj.object_id] / obj.quantity for obj in objects if obj.object_id in quantities}
            objects = tuple(replace(obj, quantity=quantities[obj.object_id]) if obj.object_id in quantities else obj for obj in objects)
            entries = [replace(entry, amount=(entry.amount * ratios[entry.object_id]
                * (D("1.03") if entry.element_id == "food_material" else D(1))).quantize(D("0.01"))) for entry in entries]
            pool_amounts = {"pool-kitchen-labor": "18400", "pool-rent": "12000", "pool-energy": "4500", "pool-platform": "2480"}
            pools = tuple(replace(pool, amount=_scaled(pool_amounts[pool.pool_id], factor)) for pool in pools)
            drivers = tuple(replace(driver, values={key: value * ratios[key] for key, value in driver.values.items()}) for driver in drivers)
        objects = tuple(replace(obj, source_refs=(_ref(dataset_id, "sample://restaurant/scenario-assumptions",
            "quantity,selling_price", f"period={period}&scenario={scenario}&object={obj.object_id}",
            "教学样本，收入与销量均为明确设定值，不是企业实测数据"),)) for obj in objects)
        benchmarks = ()
        if scenario == "actual":
            benchmarks = tuple(CostBenchmark(
                object_id=object_id, scenario="budget",
                total_cost=_scaled(amount, D("1.04")), currency="CNY",
                source_refs=(_ref(dataset_id, "sample://restaurant/budget", "budget_total", period),),
            ) for object_id, amount in (
                ("dish-rice", "26400"), ("dish-noodle", "22600"), ("dish-soup", "10600")))
        return CostingInput(
            dataset_id=dataset_id, plugin_id=self.metadata.plugin_id, period=period,
            scenario=scenario, currency="CNY", objects=objects, entries=tuple(entries),
            pools=pools, drivers=drivers, rules=rules, benchmarks=benchmarks,
            preflight_quality=({"code": "synthetic_sample", "severity": "info",
                "message": "餐饮数据为教学样本；两个月份为独立设定情景；预算成本倍率1.04、标准倍率0.98，人工变动比例0.35、能源0.70均为显式演示假设。"},),
            method="restaurant_multi_driver_allocation",
        )


class ManufacturingPlugin:
    metadata = CostingPluginMetadata(
        plugin_id="manufacturing", name="制造业成本适配器", version="1.0.0",
        industry="制造业", capabilities=("direct_material", "labor", "machine_hours",
                                         "multi_stage_allocation", "standard_variance"),
    )

    def describe(self, dataset: CostingDatasetDefinition) -> Mapping[str, Any]:
        return {
            "dataset_id": dataset.dataset_id, "plugin_id": self.metadata.plugin_id,
            "label": dataset.label, "version": dataset.version, "industry": "制造业",
            "currency": "CNY", "quantity_unit": "件",
            "periods": ["2026-05", "2026-06"],
            "scenarios": ["actual", "budget", "standard"],
            "data_kind": "synthetic",
            "scenario_basis": "显式设定的教学样本；actual 是账簿情景名，不代表真实企业实绩；预算和标准使用已声明演示假设",
            "objects": ["精密泵体", "控制阀组件"],
            "cost_objects": [{"object_id": key, "name": name} for key, name in (("product-pump", "精密泵体"), ("product-valve", "控制阀组件"))],
            "description": "模拟维护中心→生产线→产品的两阶段制造费用分配",
        }

    def load(self, dataset: CostingDatasetDefinition, period: str, scenario: str) -> CostingInput:
        if period not in {"2026-05", "2026-06"}:
            raise FileNotFoundError(f"制造业样例没有 {period} 期间数据")
        if scenario not in {"actual", "budget", "standard"}:
            raise ValueError("制造业样例支持 actual、budget、standard 情景")
        factor = D("1") if scenario == "actual" else (D("1.06") if scenario == "budget" else D("0.97"))
        dataset_id = dataset.dataset_id
        objects = (
            CostObject(object_id="product-pump", name="精密泵体", kind="product",
                       quantity=D("1000"), quantity_unit="件", currency="CNY", selling_price=D("145")),
            CostObject(object_id="product-valve", name="控制阀组件", kind="product",
                       quantity=D("800"), quantity_unit="件", currency="CNY", selling_price=D("180")),
            CostObject(object_id="cc-maintenance", name="设备维护中心", kind="cost_center", currency="CNY"),
            CostObject(object_id="cc-line-a", name="机加工线A", kind="cost_center", currency="CNY"),
            CostObject(object_id="cc-line-b", name="装配线B", kind="cost_center", currency="CNY"),
        )
        entries = (
            CostEntry(entry_id="pump-material", object_id="product-pump", element_id="direct_material",
                      amount=_scaled("52000", factor), currency="CNY", cost_type="direct", behavior="variable",
                      quantity=D("52000"), unit_rate=D("1.00"), unit="kg",
                      source_refs=(_ref(dataset_id, "sample://manufacturing/material-issue", "amount", "product=pump"),)),
            CostEntry(entry_id="valve-material", object_id="product-valve", element_id="direct_material",
                      amount=_scaled("44800", factor), currency="CNY", cost_type="direct", behavior="variable",
                      quantity=D("40000"), unit_rate=D("1.12"), unit="kg",
                      source_refs=(_ref(dataset_id, "sample://manufacturing/material-issue", "amount", "product=valve"),)),
            CostEntry(entry_id="pump-direct-labor", object_id="product-pump", element_id="direct_labor",
                      amount=_scaled("14000", factor), currency="CNY", cost_type="direct", behavior="variable",
                      source_refs=(_ref(dataset_id, "sample://manufacturing/time-sheet", "labor_cost", "product=pump"),)),
            CostEntry(entry_id="valve-direct-labor", object_id="product-valve", element_id="direct_labor",
                      amount=_scaled("12800", factor), currency="CNY", cost_type="direct", behavior="variable",
                      source_refs=(_ref(dataset_id, "sample://manufacturing/time-sheet", "labor_cost", "product=valve"),)),
        )
        pools = (
            CostPool(pool_id="pool-maintenance", name="设备维护与折旧", owner_id="cc-maintenance",
                     amount=_scaled("10000", factor), currency="CNY", element_id="maintenance",
                     behavior="fixed", source_refs=(_ref(dataset_id, "sample://manufacturing/maintenance", "amount", period),)),
            CostPool(pool_id="pool-line-a", name="机加工线A制造费用", owner_id="cc-line-a",
                     amount=_scaled("8000", factor), currency="CNY", element_id="machining_overhead",
                     behavior="semi_variable", variable_fraction=D("0.40"), include_incoming=True,
                     source_refs=(_ref(dataset_id, "sample://manufacturing/line-a-ledger", "amount", period),
                                  _ref(dataset_id, "sample://manufacturing/assumptions", "variable_fraction", period, "教学假设：机加工线变动比例 0.40"))),
            CostPool(pool_id="pool-line-b", name="装配线B制造费用", owner_id="cc-line-b",
                     amount=_scaled("6000", factor), currency="CNY", element_id="assembly_overhead",
                     behavior="semi_variable", variable_fraction=D("0.60"), include_incoming=True,
                     source_refs=(_ref(dataset_id, "sample://manufacturing/line-b-ledger", "amount", period),
                                  _ref(dataset_id, "sample://manufacturing/assumptions", "variable_fraction", period, "教学假设：装配线变动比例 0.60"))),
        )
        drivers = (
            CostDriver(driver_id="maintenance-machine-hours", name="维护设备工时", unit="机器小时",
                       values={"cc-line-a": D("600"), "cc-line-b": D("400")}),
            CostDriver(driver_id="line-a-machine-hours", name="机加工线A产品工时", unit="机器小时",
                       values={"product-pump": D("700"), "product-valve": D("300")}),
            CostDriver(driver_id="line-b-machine-hours", name="装配线B产品工时", unit="机器小时",
                       values={"product-pump": D("400"), "product-valve": D("600")}),
        )
        rules = (
            AllocationRule(rule_id="maintenance-to-lines", pool_id="pool-maintenance",
                           target_ids=("cc-line-a", "cc-line-b"), method="driver",
                           driver_id="maintenance-machine-hours", stage=1,
                           description="第一阶段：按机器工时将维护费用分配至生产线"),
            AllocationRule(rule_id="line-a-to-products", pool_id="pool-line-a",
                           target_ids=("product-pump", "product-valve"), method="driver",
                           driver_id="line-a-machine-hours", stage=2,
                           description="第二阶段：按机加工工时分配生产线A费用"),
            AllocationRule(rule_id="line-b-to-products", pool_id="pool-line-b",
                           target_ids=("product-pump", "product-valve"), method="driver",
                           driver_id="line-b-machine-hours", stage=2,
                           description="第二阶段：按装配工时分配生产线B费用"),
        )
        if period == "2026-06":
            quantities = {"product-pump": D("1100"), "product-valve": D("760")}
            objects = tuple(replace(obj, quantity=quantities[obj.object_id]) if obj.object_id in quantities else obj for obj in objects)
            material = {"pump-material": (D("57000"), D("1.03")), "valve-material": (D("37000"), D("1.09"))}
            labor = {"pump-direct-labor": D("15400"), "valve-direct-labor": D("12500")}
            revised = []
            for entry in entries:
                if entry.entry_id in material:
                    quantity, rate = material[entry.entry_id]
                    revised.append(replace(entry, quantity=quantity, unit_rate=rate * factor,
                                           amount=quantity * rate * factor))
                else:
                    revised.append(replace(entry, amount=labor[entry.entry_id] * factor))
            entries = tuple(revised)
            amounts = {"pool-maintenance": "10200", "pool-line-a": "8300", "pool-line-b": "6100"}
            pools = tuple(replace(pool, amount=_scaled(amounts[pool.pool_id], factor)) for pool in pools)
            values = {"maintenance-machine-hours": {"cc-line-a": D(630), "cc-line-b": D(370)},
                      "line-a-machine-hours": {"product-pump": D(770), "product-valve": D(285)},
                      "line-b-machine-hours": {"product-pump": D(440), "product-valve": D(570)}}
            drivers = tuple(replace(driver, values=values[driver.driver_id]) for driver in drivers)
        else:
            entries = tuple(replace(entry, unit_rate=entry.amount / entry.quantity)
                            if entry.quantity is not None and entry.quantity != 0 else entry for entry in entries)
        objects = tuple(replace(obj, source_refs=(_ref(dataset_id, "sample://manufacturing/scenario-assumptions",
            "quantity,selling_price", f"period={period}&scenario={scenario}&object={obj.object_id}",
            "教学样本，产量与售价为独立设定值，不是企业实测数据"),)) for obj in objects)
        entries = tuple(replace(entry, source_refs=tuple(replace(ref,
            selector=f"period={period}&scenario={scenario}&{ref.selector}") for ref in entry.source_refs)) for entry in entries)
        drivers = tuple(replace(driver, source_refs=(_ref(dataset_id,
            "sample://manufacturing/machine-hours", driver.driver_id,
            f"period={period}&scenario={scenario}"),)) for driver in drivers)
        standards = (
            # Illustrative standard material usage and rate, with explicit sample provenance.
            StandardCost(object_id="product-pump", element_id="direct_material",
                         quantity_per_output=D("50"), quantity_unit="kg",
                         standard_rate=D("1.00"), currency="CNY",
                         source_refs=(_ref(dataset_id, "sample://manufacturing/standard-cost", "rate", "pump"),)),
            StandardCost(object_id="product-valve", element_id="direct_material",
                         quantity_per_output=D("40"), quantity_unit="kg",
                         standard_rate=D("1.10"), currency="CNY",
                         source_refs=(_ref(dataset_id, "sample://manufacturing/standard-cost", "rate", "valve"),)),
        )
        benchmarks = ()
        if scenario == "actual":
            benchmarks = (
                CostBenchmark(object_id="product-pump", scenario="budget", total_cost=_scaled("85000", D("1.06")),
                              currency="CNY", source_refs=(_ref(dataset_id, "sample://manufacturing/budget", "total", "pump"),)),
                CostBenchmark(object_id="product-valve", scenario="budget", total_cost=_scaled("76000", D("1.06")),
                              currency="CNY", source_refs=(_ref(dataset_id, "sample://manufacturing/budget", "total", "valve"),)),
            )
        return CostingInput(
            dataset_id=dataset_id, plugin_id=self.metadata.plugin_id, period=period,
            scenario=scenario, currency="CNY", objects=objects, entries=entries,
            pools=pools, drivers=drivers, rules=rules, standard_costs=standards,
            benchmarks=benchmarks,
            preflight_quality=({"code": "synthetic_sample", "severity": "info",
                "message": "制造业数据为教学样本；两个月份为独立设定情景；预算成本倍率1.06、标准倍率0.97，机加工/装配线变动比例0.40/0.60为显式演示假设。"},),
            method="manufacturing_two_stage_machine_hour_allocation",
        )


class PharmaCostingPlugin:
    metadata = CostingPluginMetadata(
        plugin_id="pharma-costing", name="药企成本适配器", version="1.0.0",
        industry="医药制造", capabilities=("pharma_csv", "material_labor_manufacturing",
                                           "legacy_source_traceability"),
    )

    def __init__(self, raw_root: Path):
        self.raw_root = Path(raw_root)

    def describe(self, dataset: CostingDatasetDefinition) -> Mapping[str, Any]:
        from industry_plugins.pharma.adapter import PharmaAdapter, default_context
        context = default_context(dataset.dataset_id)
        try:
            observations = PharmaAdapter(raw_root=self.raw_root, context=context).load_observations()
            selected = [item for item in observations if item.organization == context.self_org]
            error = None
        except (OSError, ValueError) as exc:
            selected, error = [], type(exc).__name__
        names = sorted({item.cost_object for item in selected})
        return {
            "dataset_id": dataset.dataset_id, "plugin_id": self.metadata.plugin_id,
            "label": dataset.label, "version": dataset.version, "industry": "医药制造",
            "currency": context.currency, "quantity_unit": context.quantity_unit,
            "objects": names, "cost_objects": [{"object_id": name, "name": name} for name in names],
            "periods": sorted({item.period for item in selected}),
            "scenarios": sorted({item.scenario for item in selected}),
            "periods_by_scenario": {scenario: sorted({item.period for item in selected if item.scenario == scenario})
                                    for scenario in sorted({item.scenario for item in selected})},
            "data_kind": "source_csv", "availability": "available" if selected else "unavailable",
            "availability_reason": error or (None if selected else "source_csv_missing"),
            "description": "从实际可读原始CSV列出期间；仅成本资料，没有销售收入或固定/变动比例时不作盈利、保本推断。",
        }

    def load(self, dataset: CostingDatasetDefinition, period: str, scenario: str) -> CostingInput:
        from industry_plugins.pharma.adapter import PharmaAdapter
        from industry_plugins.pharma.policy import CATEGORY_LABELS, DEFAULT_DATASET_ID
        from industry_plugins.pharma.adapter import default_context
        if scenario not in {"actual", "budget"}:
            raise ValueError("药企数据适配器仅支持 actual 和 budget")
        context = default_context(dataset.dataset_id)
        adapter = PharmaAdapter(raw_root=self.raw_root, context=context)
        observations = adapter.load_observations()
        selected = [item for item in observations
                    if item.period == period and item.scenario == scenario
                    and item.organization == context.self_org]
        if not selected:
            raise FileNotFoundError(
                f"药企原始数据中没有 {period} {scenario} 记录；请确认 raw CSV 已挂载")
        objects = []
        entries = []
        reported_totals = {}
        reported_unit_costs = {}
        quality = []
        for item in selected:
            quantity = item.quantity
            objects.append(CostObject(
                object_id=item.cost_object, name=item.cost_object, kind="drug_product",
                quantity=quantity, quantity_unit=item.quantity_unit, currency=item.currency,
                source_refs=item.sources.get("quantity", ()),
            ))
            reported_totals[item.cost_object] = item.reported_total
            reported_unit_costs[item.cost_object] = item.reported_unit_cost
            for category, value in item.unit_costs.items():
                if value is None or quantity is None:
                    raise ValueError(f"{item.cost_object} / {period} 原始成本类别 {category} 或产量缺失，无法形成完整成本账；缺失值没有填为零")
                refs = item.sources.get(f"category.{category}.unit_cost", ())
                entries.append(CostEntry(
                    entry_id=f"{item.cost_object}-{item.period}-{category}",
                    object_id=item.cost_object, element_id=category,
                    amount=value * quantity, currency=item.currency,
                    cost_type="indirect" if category == "manufacturing_overhead" else "direct",
                    behavior="unknown",
                    quantity=quantity, unit_rate=value, unit=item.quantity_unit,
                    source_refs=refs,
                ))
        return CostingInput(
            dataset_id=dataset.dataset_id, plugin_id=self.metadata.plugin_id,
            period=period, scenario=scenario, currency="CNY",
            objects=tuple(objects), entries=tuple(entries),
            reported_totals=reported_totals, reported_unit_costs=reported_unit_costs,
            preflight_quality=tuple(quality), method="pharma_source_csv_cost_projection",
        )


def configuration_sha256(schema):
    """Hash the declared policy independently from its generated dataset ID."""
    policy = {key: value for key, value in schema.items() if key != "dataset_id"}
    return hashlib.sha256(json.dumps(policy, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def read_imported_sources(root, manifest):
    """Validate only server-generated relative names before opening any source."""
    if manifest.get("storage_format") != "costing-bundle/1":
        if "storage_format" in manifest:
            raise ValueError("未知导入存储格式")
        return None
    sources = manifest.get("source_files")
    schema = manifest.get("schema", {})
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 16:
        raise ValueError("原始文件清单必须包含1至16个来源")
    texts = {}
    base = Path(root).resolve()
    for name, item in sources.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) or not isinstance(item, dict):
            raise ValueError("原始来源标识无效")
        relative = "sources/" + name + ".csv"
        if item.get("path") != relative:
            raise ValueError("原始来源路径与清单不一致")
        path = base / relative
        if (path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(base)
                or not path.is_file()):
            raise ValueError("原始来源缺失或越出数据集")
        raw = path.read_bytes()
        if len(raw) > 10 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            raise ValueError("原始来源与导入时校验值不同：" + name)
        try:
            texts[name] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("原始来源必须使用UTF-8：" + name) from exc
    if manifest.get("configuration_sha256") != configuration_sha256(schema):
        raise ValueError("规则配置与导入时校验值不同")
    return texts


class GenericCsvPlugin:
    metadata = CostingPluginMetadata(
        plugin_id="csv-generic", name="用户自定义CSV成本适配器", version="1.0.0",
        industry="自定义", capabilities=("mapped_csv", "private_dataset"),
    )

    def __init__(self, user_root: Path):
        self.user_root = Path(user_root).resolve()

    def _paths(self, dataset_id: str):
        if not dataset_id.startswith("csv-") or not all(ch.isalnum() or ch in "-_" for ch in dataset_id):
            raise ValueError("Invalid imported dataset identifier")
        base = (self.user_root / "costing_datasets").resolve()
        root_path = self.user_root / "costing_datasets" / dataset_id
        root = root_path.resolve()
        if (not base.is_relative_to(self.user_root) or root_path.is_symlink()
                or not root.is_relative_to(base)):
            raise ValueError("Imported dataset path escapes the personal workspace")
        return root, root / "manifest.json", root / "data.csv"

    def _read_dataset(self, dataset: CostingDatasetDefinition):
        root, manifest_path, data_path = self._paths(dataset.dataset_id)
        if not manifest_path.is_file():
            raise FileNotFoundError("Imported cost dataset manifest is missing")
        if manifest_path.is_symlink():
            raise ValueError("Imported dataset files cannot be symbolic links")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise ValueError("Imported dataset manifest must be an object")
        raw_schema = manifest.get("schema")
        CsvCostingAdapter.validate_schema(raw_schema)
        schema = dict(raw_schema)
        if schema.get("plugin_id", "csv-generic") != "csv-generic":
            raise ValueError("Imported dataset must use the csv-generic adapter")
        schema["dataset_id"] = dataset.dataset_id
        schema["plugin_id"] = self.metadata.plugin_id
        # A source identifier is relative to the authorized dataset, never to
        # a staging directory that disappears after the atomic import commit.
        sources = read_imported_sources(root, manifest)
        if sources is not None:
            request = CsvCostingAdapter().from_files(sources, schema, schema_uri="upload://schema")
        else:
            if not data_path.is_file() or data_path.is_symlink():
                raise ValueError("Imported cost CSV is missing or not independent")
            if ("data_sha256" in manifest and
                    manifest["data_sha256"] != hashlib.sha256(data_path.read_bytes()).hexdigest()):
                raise ValueError("原始 CSV 与导入时校验值不同")
            request = CsvCostingAdapter().from_text(data_path.read_text(encoding="utf-8-sig"), schema,
                source_uri="upload://data.csv", schema_uri="upload://schema")
            if "data_sha256" not in manifest:
                # Legacy direct registrations did not require an import hash.
                # Preserve that contract without inventing historical assurance.
                request = replace(request, preflight_quality=tuple(request.preflight_quality) + ({
                    "code": "legacy_source_hash_unavailable", "severity": "warning",
                    "message": "历史清单未保存原始CSV哈希；当前可按旧契约读取，但无法核验文件是否与历史导入时一致。",
                },))
        return manifest, request

    def describe(self, dataset: CostingDatasetDefinition) -> Mapping[str, Any]:
        manifest, request = self._read_dataset(dataset)
        return {
            "dataset_id": dataset.dataset_id, "plugin_id": self.metadata.plugin_id,
            "label": manifest.get("label", dataset.label), "version": manifest.get("version", "1"),
            "industry": manifest.get("industry", "自定义"),
            "plugin_version": str(manifest.get("schema", {}).get("plugin_version", self.metadata.version)),
            "configuration_sha256": configuration_sha256(manifest["schema"]),
            "source_integrity_status": ("matches_import_hashes" if manifest.get("storage_format") == "costing-bundle/1"
                                        or "data_sha256" in manifest else "legacy_hash_assurance_unavailable"),
            "currency": request.currency,
            "periods": [request.period], "scenarios": [request.scenario],
            "objects": [obj.name for obj in request.objects],
            "cost_objects": [{"object_id": obj.object_id, "name": obj.name} for obj in request.objects],
            "data_kind": "user_csv", "availability": "available",
            "description": "当前用户私有工作区中的映射CSV数据集",
        }

    def load(self, dataset: CostingDatasetDefinition, period: str, scenario: str) -> CostingInput:
        manifest, request = self._read_dataset(dataset)
        if request.period != period:
            raise FileNotFoundError(f"Imported dataset only contains period {request.period}")
        if request.scenario != scenario:
            raise FileNotFoundError(f"Imported dataset only contains scenario {request.scenario}")
        return request
