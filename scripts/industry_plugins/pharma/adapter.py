"""Map pharma source rows to neutral observations; never calculate indicators."""
from __future__ import annotations

import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

from cost_core.models import AnalysisInput, CostObservation, DatasetContext, SourceRef
from .policy import (ACTUAL_FILES, BUDGET_FILES, CATEGORY_COLUMNS, CATEGORY_LABELS,
                     CURRENCY, DEFAULT_DATASET_ID, PEER_ENTITY, PLUGIN_ID,
                     PRODUCT_SPECS, QUANTITY_UNIT, SELF_ENTITY)

Rows = Sequence[Mapping[str, object]]
RowProvider = Callable[[str], Rows]
_MISSING = {"", "—", "-", "–", "N/A", "无"}
_NUMBER = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")
_GROUPED = re.compile(r"^[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


class PharmaDataError(ValueError):
    """A source row cannot be safely identified or parsed."""


def default_context(dataset_id: str = DEFAULT_DATASET_ID) -> DatasetContext:
    return DatasetContext(dataset_id=dataset_id, plugin_id=PLUGIN_ID,
                          currency=CURRENCY, quantity_unit=QUANTITY_UNIT,
                          self_org=SELF_ENTITY, peer_org=PEER_ENTITY,
                          category_labels=dict(CATEGORY_LABELS))


def _period(value: object) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", value):
        raise PharmaDataError(f"月份不是合法YYYY-MM: {value!r}")
    return value


def _decimal(value: object, *, filename: str, field: str, selector: str) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PharmaDataError(f"{filename} {selector}: {field}不能为布尔值")
    text = str(value).strip()
    if text in _MISSING:
        return None
    if not (_NUMBER.fullmatch(text) or _GROUPED.fullmatch(text)):
        raise PharmaDataError(f"{filename} {selector}: {field}不是有效数值: {text!r}")
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise PharmaDataError(f"{filename} {selector}: {field}不是有效数值") from exc
    if not number.is_finite():
        raise PharmaDataError(f"{filename} {selector}: {field}必须为有限数")
    return number


class PharmaAdapter:
    def __init__(self, *, context: Optional[DatasetContext] = None,
                 row_provider: Optional[RowProvider] = None,
                 raw_root: Optional[Path] = None,
                 synthetic_files: Iterable[str] = (),
                 input_periods: Optional[Mapping[str, Sequence[str]]] = None):
        self.context = context or default_context()
        self.raw_root = Path(raw_root) if raw_root is not None else Path(__file__).resolve().parents[3] / "raw"
        self.row_provider = row_provider if row_provider is not None else self._csv_rows
        self.synthetic_files = frozenset(synthetic_files)
        self.input_periods = {k: tuple(v) for k, v in (input_periods or {}).items()}
        if (self.context.plugin_id != PLUGIN_ID or self.context.currency != CURRENCY
                or self.context.quantity_unit != QUANTITY_UNIT
                or set(self.context.category_labels) != set(CATEGORY_COLUMNS)):
            raise PharmaDataError("Pharma context的插件、币种、数量单位或类别集合不兼容")

    def _csv_rows(self, filename: str) -> Rows:
        with (self.raw_root / filename).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)):
                raise PharmaDataError(f"{filename}: CSV表头重复")
            return list(reader)

    def normalize_rows(self, filename: str, rows: Rows, *, organization: str,
                       scenario: str = "actual", allow_inferred_identity: bool = False) -> tuple[CostObservation, ...]:
        if scenario not in {"actual", "budget"}:
            raise PharmaDataError(f"未知scenario: {scenario}")
        prefix = "预算" if scenario == "budget" else ""
        output = []
        seen = set()
        specs: dict[tuple[str, str, str], str] = {}
        file_synthetic = filename in self.synthetic_files
        # Match CSV reader coordinates throughout the application: header=1.
        for index, row in enumerate(rows, 2):
            # Period projections carry per-row windows: current and previous
            # aliases can share a CSV filename but have different real inputs.
            row_kind = row.get("__source_kind")
            if row_kind is not None and row_kind not in {"raw", "synthetic", "inferred"}:
                raise PharmaDataError(f"{filename} 记录{index}: 无效的行来源类型")
            synthetic = row_kind == "synthetic" if row_kind is not None else file_synthetic
            periods = row.get("__input_periods", self.input_periods.get(filename, ()))
            if not isinstance(periods, (tuple, list)):
                raise PharmaDataError(f"{filename} 记录{index}: 输入月份须为tuple/list")
            periods = tuple(_period(value) for value in periods)
            if len(periods) != len(set(periods)):
                raise PharmaDataError(f"{filename} 记录{index}: 输入月份重复")
            product = str(row.get("产品名称") or "").strip()
            month = _period(row.get("月份"))
            if not product:
                raise PharmaDataError(f"{filename} 记录{index}: 产品名称缺失")
            source_org = str(row.get("工厂") or "").strip()
            spec = str(row.get("产品规格") or "").strip()
            inferred = not source_org or not spec
            if not source_org:
                if not allow_inferred_identity:
                    raise PharmaDataError(f"{filename} 记录{index}: 工厂缺失")
                source_org = organization
            if source_org != organization:
                raise PharmaDataError(f"{filename} 记录{index}: 工厂{source_org!r}与文件角色{organization!r}冲突")
            if not spec and not allow_inferred_identity:
                raise PharmaDataError(f"{filename} 记录{index}: 产品规格缺失")
            if not spec and allow_inferred_identity:
                spec = PRODUCT_SPECS.get(product, "")
            key = (source_org, product, month, scenario)
            if key in seen:
                raise PharmaDataError(f"{filename}: 重复观测 {key!r}，禁止静默取首行或求和")
            seen.add(key)
            spec_key = (source_org, product, scenario)
            if spec_key in specs and specs[spec_key] != spec:
                raise PharmaDataError(f"{filename}: 同名产品存在多个规格，需显式独立cost_object: {product}")
            specs[spec_key] = spec
            selector = f"工厂={source_org}&产品名称={product}&产品规格={spec or '未提供(兼容行)'}"
            kind = "synthetic" if synthetic else ("inferred" if inferred or row_kind == "inferred" else "raw")
            if synthetic:
                selector += f"&synthetic_join_label={month}"
                provenance = ("由期间聚合器注入的合成行，月份仅为兼容连接标签；原始输入来自此文件。"
                              + ("输入月份=" + ",".join(periods)
                                 if periods else "覆盖接口未提供确切输入月份，不伪造原始月度坐标。"))
            else:
                selector += f"&月份={month}&CSV记录={index}"
                provenance = "文件角色推断缺失的组织/规格身份；原始数值未改写。" if inferred else ""
            columns = {"quantity": prefix + "产量(盒)",
                       "unit_cost": prefix + "单位成本(元/盒)",
                       "total_cost": prefix + "总成本(元)"}
            columns.update({f"category.{key}.unit_cost": prefix + col
                            for key, col in CATEGORY_COLUMNS.items()})
            refs = {key: (SourceRef(dataset_id=self.context.dataset_id,
                                    source_uri="raw/" + filename, field=col,
                                    selector=selector, kind=kind, formula=provenance),)
                    for key, col in columns.items()}
            row_sources = row.get("__core_sources")
            if row_sources is not None:
                if not isinstance(row_sources, Mapping):
                    raise PharmaDataError(f"{filename} 记录{index}: 核心来源须为Mapping")
                for source_key, source_refs in row_sources.items():
                    if not isinstance(source_key, str) or not source_key or not isinstance(source_refs, (tuple, list)):
                        raise PharmaDataError(f"{filename} 记录{index}: 核心来源字段结构无效")
                    if any(not isinstance(ref, SourceRef) or ref.dataset_id != self.context.dataset_id
                           for ref in source_refs):
                        raise PharmaDataError(f"{filename} 记录{index}: 核心来源类型或数据集不匹配")
                    # Keep original monthly references and derived formulas
                    # verbatim; no invented physical row for the join alias.
                    refs[source_key] = tuple(source_refs)
            values = {key: _decimal(row.get(col), filename=filename, field=col, selector=selector)
                      for key, col in columns.items()}
            if values["quantity"] is not None and values["quantity"] < 0:
                raise PharmaDataError(f"{filename} {selector}: 产量为负，未定义药企冲销产量口径")
            output.append(CostObservation(
                dataset_id=self.context.dataset_id, organization=source_org,
                cost_object=product, period=month, quantity=values["quantity"],
                unit_costs={key: values[f"category.{key}.unit_cost"] for key in CATEGORY_COLUMNS},
                reported_unit_cost=values["unit_cost"], reported_total=values["total_cost"],
                currency=self.context.currency, quantity_unit=self.context.quantity_unit,
                sources=refs, scenario=scenario, source_kind=kind,
                input_periods=periods if synthetic else (month,)))
        return tuple(output)

    def load_observations(self) -> tuple[CostObservation, ...]:
        output = []
        for scenario, files in (("actual", ACTUAL_FILES), ("budget", BUDGET_FILES)):
            for (organization, year), filename in files.items():
                observations = self.normalize_rows(filename, self.row_provider(filename),
                                                   organization=organization, scenario=scenario)
                for observation in observations:
                    if not observation.period.startswith(str(year) + "-"):
                        raise PharmaDataError(f"{filename}: 观测年份与文件角色不一致")
                output.extend(observations)
        return tuple(output)

    def load_snapshot(self, product: str, month: str) -> AnalysisInput:
        """Read through the injected provider, including explicit empty overrides.

        No persistent cache and no aggregation: synthetic aliases stay aliases.
        """
        month = _period(month)
        year, number = int(month[:4]), int(month[5:])
        previous_month = f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"
        year_ago_month = f"{year - 1}-{number:02d}"
        loaded: dict[tuple[str, str, str], tuple[CostObservation, ...]] = {}

        def group(organization: str, source_year: int, scenario: str):
            files = BUDGET_FILES if scenario == "budget" else ACTUAL_FILES
            filename = files.get((organization, source_year))
            if filename is None:
                return ()
            key = (filename, organization, scenario)
            if key not in loaded:
                loaded[key] = self.normalize_rows(filename, self.row_provider(filename),
                                                   organization=organization, scenario=scenario,
                                                   allow_inferred_identity=True)
            return loaded[key]

        def pick(observations, label):
            return next((o for o in observations if o.cost_object == product and o.period == label), None)

        current_group = group(self.context.self_org, year, "actual")
        current = pick(current_group, month)
        if current is None:
            filename = ACTUAL_FILES.get((self.context.self_org, year), "csv/cost_data/未提供年份成本汇总.csv")
            ref = SourceRef(dataset_id=self.context.dataset_id, source_uri="raw/" + filename,
                            selector=f"工厂={self.context.self_org}&产品名称={product}&月份={month}",
                            kind="missing", formula="读取结果中无匹配观测；缺失不是零。")
            current = CostObservation(dataset_id=self.context.dataset_id,
                organization=self.context.self_org, cost_object=product, period=month,
                quantity=None, unit_costs={key: None for key in CATEGORY_COLUMNS},
                reported_unit_cost=None, reported_total=None, currency=self.context.currency,
                quantity_unit=self.context.quantity_unit,
                sources={key: (ref,) for key in ["quantity", "unit_cost", "total_cost"] +
                         [f"category.{key}.unit_cost" for key in CATEGORY_COLUMNS]},
                source_kind="missing")
        previous = pick(group(self.context.self_org, int(previous_month[:4]), "actual"), previous_month)
        return AnalysisInput(context=self.context, cost_object=product, period=month,
            current=current, previous=previous,
            year_ago=pick(group(self.context.self_org, year - 1, "actual"), year_ago_month),
            budget=pick(group(self.context.self_org, year, "budget"), month),
            peer=pick(group(self.context.peer_org, year, "actual"), month),
            history=tuple(o for o in current_group if o.cost_object == product and o.period <= month))


def load_snapshot(product: str, month: str, *, row_provider: RowProvider,
                  context: Optional[DatasetContext] = None,
                  synthetic_files: Iterable[str] = (),
                  input_periods: Optional[Mapping[str, Sequence[str]]] = None) -> AnalysisInput:
    return PharmaAdapter(context=context, row_provider=row_provider,
                         synthetic_files=synthetic_files, input_periods=input_periods).load_snapshot(product, month)
