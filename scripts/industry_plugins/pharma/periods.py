# -*- coding: utf-8 -*-
"""Legacy compatibility implementation; original source materials are not included."""





































from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))
from console_io import ensure_utf8_stdout  # noqa: E402
import report_facts as RF  # noqa: E402
from cost_conventions import as_decimal  # noqa: E402
from decimal import Decimal
from cost_core import aggregation as core_aggregation
from cost_core.models import SourceRef
from .adapter import PharmaAdapter, default_context
from .policy import ACTUAL_FILES, BUDGET_FILES, CATEGORY_COLUMNS, CATEGORY_LABELS, SELF_ENTITY, PEER_ENTITY

ensure_utf8_stdout()

# 可分析的季度（数据只到 2026-06，故只有上半年两个季度）
AVAILABLE_QUARTERS = ["2026-Q1", "2026-Q2"]


def quarter_months(q: str) -> List[str]:
    """`'2026-Q2'` → `['2026-04', '2026-05', '2026-06']`"""
    y, qn = q.split("-Q")
    n = int(qn)
    if n not in (1, 2, 3, 4):
        raise ValueError(f"季度不合法：{q}")
    return [f"{y}-{m:02d}" for m in range(3 * n - 2, 3 * n + 1)]


def prev_quarter(q: str) -> Optional[str]:
    """上一个季度；跨年时返回上年 Q4。"""
    y, qn = q.split("-Q")
    n = int(qn)
    return f"{int(y) - 1}-Q4" if n == 1 else f"{y}-Q{n - 1}"


def same_quarter_last_year(q: str) -> str:
    y, qn = q.split("-Q")
    return f"{int(y) - 1}-Q{qn}"


def quarter_label(q: str) -> str:
    y, qn = q.split("-Q")
    return f"{y} 年{'一二三四'[int(qn) - 1]}季度"


# ---------------------------------------------------------------------------
# 聚合核心
# ---------------------------------------------------------------------------

def _rows(rows: List[Dict[str, str]], product: str, months: List[str]
          ) -> List[Dict[str, str]]:
    """取出该产品在这些月份的行（缺月直接跳过，由调用方判缺失）。"""
    want = set(months)
    return [r for r in rows
            if (r.get("产品名称") or "").strip() == product
            and (r.get("月份") or "").strip() in want]


def _aggregate_observation(rows, product, months, *, month_label=None, scenario="actual", organization=None):
    """Pharma schema/provenance conversion; all quantity weighting runs in core."""
    got = _rows(rows, product, months)
    if not months or len(months) != len(set(months)):
        raise ValueError("months must be non-empty and unique")
    if not got:
        return None
    organizations = {str(row.get("工厂") or "").strip() for row in got if row.get("工厂")}
    if len(organizations) > 1:
        raise ValueError("cannot aggregate multiple organizations in one pharma period")
    if organization is not None and organizations and organizations != {organization}:
        raise ValueError("source organization conflicts with the requested period role")
    organization = organization or next(iter(organizations), SELF_ENTITY)
    files = BUDGET_FILES if scenario == "budget" else ACTUAL_FILES
    year = int(months[0][:4])
    filename = files.get((organization, year), f"csv/cost_data/{organization}_{scenario}_{year}.csv")
    adapter = PharmaAdapter(context=default_context())
    # Normalize before selecting the period so physical CSV record indices stay
    # relative to the complete source file, including earlier products/months.
    normalized = adapter.normalize_rows(filename, rows, organization=organization,
                                         scenario=scenario, allow_inferred_identity=True)
    observations = tuple(row for row in normalized
                         if row.cost_object == product and row.period in set(months))
    return core_aggregation.weighted_observations(
        observations, tuple(months), period=month_label, context=adapter.context)


def _unit_from_reported_total(observation):
    # The legacy pharma quarterly contract explicitly defines unit total as
    # reported total / reported output. Core keeps both reported fields intact.
    if observation.quantity == 0 or observation.reported_total is None:
        return None
    return observation.reported_total / observation.quantity


def aggregate(rows: List[Dict[str, str]], product: str,
              months: List[str], *, organization: Optional[str] = None) -> Dict[str, Any]:
    """Complete-period quantity weighting through core; incomplete is explicit."""
    got = _rows(rows, product, months)
    have = {row["月份"] for row in got}
    missing = [month for month in months if month not in have]
    empty = {"月份数": len(got), "缺月": missing, "产量": None, "总成本": None,
             "单位成本": None, "要素单位": {label: None for label in CATEGORY_LABELS.values()},
             "自洽偏差": None, "完整": False}
    if not months or len(months) != len(set(months)):
        raise ValueError("months must be non-empty and unique")
    if missing:
        return empty
    observation = _aggregate_observation(rows, product, months, organization=organization)
    if observation is None:
        return empty
    unit = _unit_from_reported_total(observation)
    values = list(observation.unit_costs.values())
    deviation = (sum(values, Decimal(0)) - unit
                 if unit is not None and all(value is not None for value in values) else None)
    return {"月份数": len(got), "缺月": [], "产量": float(observation.quantity),
            "总成本": float(observation.reported_total) if observation.reported_total is not None else None,
            "单位成本": float(unit) if unit is not None else None,
            "要素单位": {CATEGORY_LABELS[key]: float(value) if value is not None else None
                         for key, value in observation.unit_costs.items()},
            "自洽偏差": float(deviation) if deviation is not None else None, "完整": True}


def _synth_row(rows: List[Dict[str, str]], product: str, months: List[str],
               month_label: str, *, scenario: str = "actual", organization: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Project a core weighted observation into the legacy CSV join schema."""
    got = _rows(rows, product, months)
    if not got or any(month not in {row["月份"] for row in got} for month in months):
        return None
    observation = _aggregate_observation(rows, product, months, month_label=month_label, scenario=scenario,
                                       organization=organization)
    if observation is None:
        return None
    prefix = "预算" if scenario == "budget" else ""
    base = dict(got[0])
    base["月份"] = month_label
    base[prefix + "产量(盒)"] = str(observation.quantity)
    base[prefix + "总成本(元)"] = str(observation.reported_total) if observation.reported_total is not None else ""
    unit = _unit_from_reported_total(observation)
    base[prefix + "单位成本(元/盒)"] = str(unit) if unit is not None else ""
    for key, column in CATEGORY_COLUMNS.items():
        value = observation.unit_costs[key]
        base[prefix + column] = str(value) if value is not None else ""
    sources = dict(observation.sources)
    sources["unit_cost"] = tuple(dict.fromkeys((*sources["total_cost"], *sources["quantity"],
        SourceRef(dataset_id=observation.dataset_id,
                  source_uri=f"derived:pharma-period:{product}@{month_label}", field="unit_cost",
                  selector=",".join(months), kind="derived",
                  formula="sum(reported_total) / sum(quantity); legacy pharma period policy"))))
    for key, refs in tuple(sources.items()):
        if not any(ref.kind == "derived" for ref in refs):
            formula = next((ref.formula for ref in reversed(refs) if ref.formula), "period aggregation")
            sources[key] = (*refs, SourceRef(dataset_id=observation.dataset_id,
                                            source_uri=f"derived:pharma-period:{product}@{month_label}",
                                            field=key, selector=",".join(months), kind="derived", formula=formula))
    base["__source_kind"] = "synthetic"
    base["__input_periods"] = observation.input_periods
    base["__core_sources"] = sources
    return base


def _agg_detail(rows: List[Dict[str, str]], product: str, months: List[str],
                label: str, group_key: str,
                unit_cols: List[str], sum_cols: List[str],
                avg_cols: List[str], source_file: str = "") -> List[Dict[str, str]]:
    """
    把明细表（制造费用 / 原材料 / 工时）按月聚合到季度，**按分组键分组**。

    三列各有口径，不能一刀切：
      · `sum_cols`  —— 可加的（费用总额、工时、产量、天数）→ 直接求和
      · `unit_cols` —— "每盒"量（单位费用、单位消耗成本）→ **按产量加权**
      · `avg_cols`  —— 时点/属性量（生产人数）→ **算术平均**（求和会翻三倍）

    ⚠️ 这三条规则必须写下来，否则下一个人会以为"数字为什么不是三倍"。
       尤其 `avg_cols`：人数是"月末在岗"，三个人月相加得 3 倍人力，
       算出来的效率会低到 1/3——错得离谱但看着像真的。
    """
    got = [r for r in rows
           if (r.get("产品名称") or "").strip() == product
           and (r.get("月份") or "").strip() in set(months)]
    if not got:
        return []
    csv_records = {id(row): index for index, row in enumerate(rows, 2)}

    if not months or len(months) != len(set(months)):
        raise ValueError("明细聚合的输入月份须非空且不能重复")
    # Each category repeats the same monthly production. Count that production
    # once, and fail on conflicting repeated values instead of guessing a total.
    monthly_quantity: Dict[str, Optional[Decimal]] = {}
    groups: Dict[str, List[Dict[str, str]]] = {}
    for r in got:
        period = (r.get("月份") or "").strip()
        quantity = as_decimal(r.get("产量(盒)"))
        if quantity is not None and quantity < 0:
            raise ValueError(f"{product} {period} 的明细产量不得为负")
        if period in monthly_quantity and monthly_quantity[period] != quantity:
            raise ValueError(f"{product} {period} 不同明细行的产量冲突")
        monthly_quantity[period] = quantity
        key = (r.get(group_key) or "").strip()
        if not key:
            raise ValueError(f"{product} {period} 缺少明细分组 {group_key}")
        group = groups.setdefault(key, [])
        if any((row.get("月份") or "").strip() == period for row in group):
            raise ValueError(f"{product} {period} {key} 存在重复明细行")
        group.append(r)
    complete_q = (set(monthly_quantity) == set(months)
                  and all(value is not None for value in monthly_quantity.values()))
    total_q = sum(monthly_quantity.values(), Decimal(0)) if complete_q else None

    out: List[Dict[str, str]] = []
    for key, grp in sorted(groups.items()):
        base = dict(grp[0])
        base["月份"] = label
        base[group_key] = key
        base["产量(盒)"] = str(total_q) if total_q is not None else ""
        missing = sorted(set(months) - {(r.get("月份") or "").strip() for r in grp})
        base["__missing_months"] = missing
        base["__source_kind"] = "synthetic"
        base["__input_periods"] = tuple(months)
        if source_file:
            base["__detail_input_refs"] = {
                column: [SourceRef(dataset_id=default_context().dataset_id,
                                   source_uri="raw/" + source_file, field=column,
                                   selector=(f"产品名称={product}"
                                             + (f"&{group_key}={key}" if group_key != "产品名称" else "")
                                             + f"&月份={row['月份']}&CSV记录={csv_records[id(row)]}"),
                                   kind="raw") for row in grp]
                for column in {"产量(盒)", *unit_cols, *sum_cols, *avg_cols}}
        for c in sum_cols:
            values = [as_decimal(r.get(c)) for r in grp]
            base[c] = (str(sum(values, Decimal(0))) if not missing
                       and all(v is not None for v in values) else "")
        for c in unit_cols:
            values = [as_decimal(r.get(c)) for r in grp]
            base[c] = ""
            if not missing and total_q and all(v is not None for v in values):
                numerator = sum((v * as_decimal(r.get("产量(盒)"))
                                 for v, r in zip(values, grp)), Decimal(0))
                base[c] = str(numerator / total_q)
        for c in avg_cols:
            values = [as_decimal(r.get(c)) for r in grp]
            base[c] = (str(sum(values, Decimal(0)) / Decimal(len(values)))
                       if not missing and all(v is not None for v in values) else "")
        if "生产人数(人)" in avg_cols:
            person_days = [(as_decimal(row.get("生产人数(人)")), as_decimal(row.get("工作天数(天)")))
                           for row in grp]
            base["__person_days"] = (str(sum((people * days for people, days in person_days), Decimal(0)))
                                      if not missing and all(people is not None and days is not None
                                                             for people, days in person_days) else "")
        out.append(base)
    # A material share is a quarter statistic, not the first month's attribute.
    if out and "占总材料成本比例" in out[0]:
        costs = [as_decimal(r.get("单位消耗成本(元/盒)")) for r in out]
        total = sum(costs, Decimal(0)) if all(v is not None for v in costs) else None
        for row, value in zip(out, costs):
            row["占总材料成本比例"] = (f"{value / total * 100:.2f}%"
                                      if total and value is not None else "")
    return out


def build_quarter(product: str, quarter: str,
                  workspace_root: Optional[Path] = None) -> Tuple[RF.FactSet, Dict[str, Any]]:
    if workspace_root is not None:
        with RF.data_workspace(workspace_root):
            return _build_quarter(product, quarter)
    return _build_quarter(product, quarter)


def _build_quarter(product: str, quarter: str) -> Tuple[RF.FactSet, Dict[str, Any]]:
    """
    构造**季度 FactSet**。键名与月度一致（`本月X` 语义即「本季度X」），
    因此下游 `deterministic_map` / 表格渲染 / LLM 分析 **无需改动**。

    实现要点：把三个月聚成**一行合成记录**（`_synth_row`），
    通过请求作用域 `RF.overrides()` 喂给 `report_facts.build()`——
    于是**全部派生值（环比/占比/贡献度/预算偏差）由原逻辑算出**，
    不是这里另算一套。

    返回 `(FactSet, 聚合元信息)`；元信息里带缺月、自洽偏差等，供报告如实披露。
    """
    ms = quarter_months(quarter)
    last = ms[-1]      # 期末月：报告定位、行标签、_shift_month 的基准
    prev_q = prev_quarter(quarter)
    yoy_q = same_quarter_last_year(quarter)
    prev_ms = quarter_months(prev_q) if prev_q else []
    yoy_ms = quarter_months(yoy_q)

    F26 = "csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv"
    F25 = "csv/cost_data/中药一厂_成本汇总_2025年1-6月.csv"
    FB = "csv/cost_data/中药一厂_预算数据_2026年.csv"
    FL = "csv/cost_data/中药一厂_人工工时明细_2026年1-6月.csv"
    FM = "csv/cost_data/中药一厂_制造费用明细_2026年1-6月.csv"
    FT = "csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv"
    FP = "csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv"

    r26 = RF._read(F26)
    r25 = RF._read(F25)
    rbud = RF._read(FB)
    rpeer = RF._read(FP)

    cur = aggregate(r26, product, ms)
    peer_cur = aggregate(rpeer, product, ms, organization=PEER_ENTITY)
    peer_comparable = (not cur["缺月"] and not peer_cur["缺月"]
                       and cur["单位成本"] is not None and peer_cur["单位成本"] is not None)
    peer_row = _synth_row(rpeer, product, ms, last, organization=PEER_ENTITY) if peer_comparable else None
    prv = aggregate(r26, product, prev_ms) if prev_ms else None
    yoy = aggregate(r25, product, yoy_ms)

    # ---- 构造合成行：本期 / 上季 / 去年同期各一行 ----
    cur_row = _synth_row(r26, product, ms, ms[-1])
    if cur_row is None:
        raise ValueError(f"{product} 在 {quarter} 无法完整聚合，缺月={cur['缺月']}")
    rows26: List[Dict[str, str]] = [cur_row]
    if prv is not None:
        prow = _synth_row(r26, product, prev_ms, prev_ms[-1])
        if prow:
            rows26.append(prow)
    rows25: List[Dict[str, str]] = []
    # ⚠️ 标签必须是 `year_ago_month`（如 2025-06），不是季末月——
    #    report_facts 用 `"2025-" + month[5:]` 去查去年同期，
    #    标错就查不到 → 同比静默变空。
    yrow = _synth_row(r25, product, yoy_ms, RF._shift_month(last, -12) or yoy_ms[-1])
    if yrow:
        rows25.append(yrow)

    # ---- 预算：按季度求和后合成一行 ----
    brows = [r for r in rbud if (r.get("产品名称") or "").strip() == product
             and (r.get("月份") or "").strip() in set(ms)]
    budget_rows: List[Dict[str, str]] = []
    if brows:
        budget_row = _synth_row(rbud, product, ms, last, scenario="budget")
        budget_rows = [budget_row] if budget_row is not None else []

    # ---- 明细表：按季度聚合，并**按 report_facts 期望的月份标签打标** ----
    #
    # ⚠️ 标签为什么是"上月"而不是"上季末月"：
    #    report_facts 内部用 `_shift_month(month, -1)` 去查"上月"行，
    #    季度分析里 month=期末月（如 2026-06），它就去查 2026-05。
    #    若我们把上季行标成 2026-03，**查不到 → 环比静默变空**。
    #    所以这里把上季行标成它要找的那个字符串。
    #    ⚠️ 这个标签只是**内部连接的键**，用户看不到；
    #       模板里印的是「上季」（见季度模板的措辞），不会撒谎。
    prev_label = RF._shift_month(last, -1) or last

    rows26.append(_synth_row(r26, product, prev_ms, prev_label)) if prev_ms else None

    def _detail(src: List[Dict[str, str]], group_key: str, unit_cols, sum_cols,
                avg_cols, source_file) -> List[Dict[str, str]]:
        cur = _agg_detail(src, product, ms, last, group_key, unit_cols, sum_cols, avg_cols, source_file)
        if prev_ms:
            prev = _agg_detail(src, product, prev_ms, prev_label, group_key,
                               unit_cols, sum_cols, avg_cols, source_file)
            cur = cur + prev
        return cur

    FM_rows = _detail(RF._read(FM), "费用类别", ["单位费用(元/盒)"],
                      ["费用总额(元)"], [], FM)
    FT_rows = _detail(RF._read(FT), "原材料名称", ["单位消耗成本(元/盒)"],
                      ["原材料总成本(元)"], [], FT)
    FL_rows = _detail(RF._read(FL), "产品名称", [],
                      ["直接人工总额(元)", "总工时(小时)", "工作天数(天)"],
                      ["生产人数(人)"], FL)

    with RF.overrides({
        F26: [r for r in rows26 if r], F25: rows25, FB: budget_rows,
        FM: FM_rows, FT: FT_rows, FL: FL_rows,
        FP: [peer_row] if peer_row else [],
    }):
        fs = RF.build(product, last)
    # Synthetic labels are join keys for quarter comparisons, never real monthly observations.
    fs.monthly = RF.build(product, last).monthly
    fs.period_kind = "quarterly"
    fs.period_label = quarter
    fs.input_periods = tuple(ms)
    if not peer_comparable:
        fs.missing.append("季度对标不可比：双方须覆盖同一季度全部月份且具备有效产量与总成本")

    label = quarter_label(quarter)

    meta = {
        "quarter": quarter, "label": label, "months": ms,
        "prev_quarter": prev_q, "yoy_quarter": yoy_q,
        "missing_months": cur["缺月"], "n_months": cur["月份数"],
        "self_consistency_dev": cur["自洽偏差"],
        "peer_missing_months": peer_cur["缺月"], "peer_comparable": peer_comparable,
        "has_prev": bool(prv and prv["单位成本"] is not None),
        "has_yoy": yoy["单位成本"] is not None,
    }
    return fs, meta


def _main() -> int:
    ap = argparse.ArgumentParser(description="季度聚合（赛题 5.1.3）")
    ap.add_argument("--product", required=True)
    ap.add_argument("--quarter", required=True, help="如 2026-Q2")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    fs, meta = build_quarter(a.product, a.quarter)
    if a.json:
        print(json.dumps({
            "meta": meta,
            "facts": {k: {"value": v.value, "display": v.display,
                          "unit": v.unit, "formula": v.formula}
                      for k, v in fs.facts.items()},
            "missing": fs.missing,
        }, ensure_ascii=False, indent=1))
        return 0

    print("=" * 74)
    print(f"{meta['label']}　{fs.product}　（{meta['months'][0]} ~ {meta['months'][-1]}）")
    print("=" * 74)
    for k, v in fs.facts.items():
        f = f"　= {v.formula}" if v.formula else ""
        print(f"  {k:<16} {v.display:>10} {v.unit}{f}")
    dev = meta["self_consistency_dev"]
    if dev is not None and abs(dev) > 0.005:
        print(f"\n  ⚠️ 自洽偏差 {dev:+.4f}：三要素之和与单位成本不一致"
              f"（源数据本身如此，见 wiki 的 Disputed 登记）")
    for m in fs.missing:
        print(f"  ⚠️ {m}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
