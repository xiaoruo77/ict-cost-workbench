# -*- coding: utf-8 -*-
"""Legacy compatibility implementation; original source materials are not included."""



































from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from decimal import Decimal
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from console_io import ensure_utf8_stdout  # noqa: E402
from errors import DataMissing, ConfigError  # noqa: E402
from cost_conventions import as_decimal, compare_cost, format_number, DIFF_NOTE  # noqa: E402
from .policy import LEGACY_DIRECTIONS  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parents[2]
WIKI_ROOT = HERE.parent
RAW = WIKI_ROOT / "raw"

UNIT_PER_BOX = {"银黄口服液": "支", "板蓝根颗粒": "袋", "六味地黄胶囊": "粒"}
BOX_PACK = {"银黄口服液": 10, "板蓝根颗粒": 20, "六味地黄胶囊": 60}

# 制造费用五类 ↔ 模板措辞。CSV 列名是「人工(间接)」，模板叫「间接人工」。
#   [报告模板契约.md:3.3] 明确提示过这处名称映射。
MFG_TEMPLATE_NAMES = {
    "折旧费": "折旧费",
    "动力费(水电气)": "动力费(水电气)",
    "人工(间接)": "间接人工",
    "检验费": "检验费",
    "其他制造费用": "其他制造费用",
}


def _coord(fname: str, col: str, rows: str) -> str:
    return f"[{fname}:{col}:{rows}]"


def _num(v: Any) -> Optional[float]:
    number = as_decimal(v)
    return float(number) if number is not None else None


def _fmt(v: Optional[float], nd: int = 2) -> str:
    """按报表小数位舍入显示；不得用此显示值构造可复算公式。"""
    return format_number(v, places=nd)


def _formula_number(value: Any) -> str:
    """Serialize an unrounded operand, independently of table display precision.

    Decimal/source strings retain every digit. Legacy detail calculations store
    floats, whose shortest round-trip string retains their actual operand value.
    Never quantize here: a small nonzero denominator must not print as zero.
    """
    number = as_decimal(value)
    if number is None:
        raise ValueError("公式操作数必须是有限数值")
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pct_formula(current: Any, baseline: Any) -> str:
    """A percentage fact stores percentage points, so its formula includes 100."""
    cur, prev = _formula_number(current), _formula_number(baseline)
    return f"({cur} − {prev}) ÷ {prev} × 100"


def _pct(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    """环比/同比 %。分母为 0 或缺失时返回 None（缺失≠0）。"""
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / prev * 100.0


# 行覆盖：让**季度/专题**分析能把"聚合后的合成行"喂进来，
# 从而**复用本模块的全部推导逻辑**（环比 / 占比 / 贡献度 / 预算偏差…）。
#
# ⚠️ 为什么用覆盖而不是另写一套聚合推导：
#    另写一套 = 两处口径，迟早漂移；而"环比""贡献度"这类算法
#    一旦两处实现不同，报告之间就会出现**互相矛盾但各自合法**的数——
#    本库最贵的一类错。覆盖机制让下游逻辑**只有一份**。
_ROW_CONTEXT: ContextVar = ContextVar("pharma_row_overrides", default=(None, {}))
_DATA_WORKSPACE: ContextVar = ContextVar("pharma_data_workspace", default=None)


def current_workspace() -> Path:
    return _DATA_WORKSPACE.get() or WIKI_ROOT


@contextmanager
def data_workspace(root: Path):
    token = _DATA_WORKSPACE.set(Path(root).resolve())
    try:
        yield
    finally:
        _DATA_WORKSPACE.reset(token)


def __getattr__(name: str):
    # Read compatibility for the historical facade. There is deliberately no
    # writable module-wide override: concurrent threads/tasks see their context.
    if name == "_OVERRIDE":
        return _ROW_CONTEXT.get()[0]
    if name == "_OVERRIDE_INPUT_PERIODS":
        return _ROW_CONTEXT.get()[1]
    raise AttributeError(name)


def use_overrides(mapping: Optional[Dict[str, List[Dict[str, str]]]], *,
                  input_periods: Optional[Dict[str, List[str]]] = None) -> None:
    """
    临时替换若干 CSV 的读取结果。传 `None` 恢复读真实文件。

    用法（季度分析）：
        use_overrides({"csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv": 合成行})
        fs = build(product, month)
        use_overrides(None)          # 用完必须恢复，否则污染后续调用
    """
    _ROW_CONTEXT.set((mapping, {key: tuple(periods) for key, periods in
                                (input_periods or {}).items()} if mapping is not None else {}))


@contextmanager
def overrides(mapping: Dict[str, List[Dict[str, str]]], *, input_periods=None):
    """Scope synthetic rows to this request, restoring nested scopes on error."""
    token = _ROW_CONTEXT.set((mapping, {key: tuple(periods) for key, periods in
                                        (input_periods or {}).items()}))
    try:
        yield
    finally:
        _ROW_CONTEXT.reset(token)


def _read(name: str) -> List[Dict[str, str]]:
    mapping = _ROW_CONTEXT.get()[0]
    if mapping is not None and name in mapping:
        return mapping[name]
    raw_root = current_workspace() / "raw" if _DATA_WORKSPACE.get() is not None else RAW
    from overlay import contained_path
    p = contained_path(raw_root, name)
    if not p.exists():
        # ⚠️ 这里曾是 `raise SystemExit`。SystemExit 继承 BaseException，
        #    app.py 的 `except Exception` 拦不住 ⇒ 6 个看板接口
        #    （overview/trend/heatmap/products/benchmark/decide）会**直接断连**，
        #    前端显示"网络错误"，把"缺个 CSV"误报成"服务挂了"。
        #    改用可捕获的 DataMissing，HTTP 层才能回一句人话。
        raise DataMissing(
            f"raw 文件缺失: {name}（找的是 {p}）",
            hint="确认数据已放在 raw/csv/ 下；自造的样例工作区可跑 "
                 "python scripts/make_sample_workspace.py 生成")
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _pick(rows: List[Dict[str, str]], **kw) -> Optional[Dict[str, str]]:
    for r in rows:
        if all((r.get(k) or "").strip() == str(v) for k, v in kw.items()):
            return r
    return None


@dataclass
class Fact:
    """一个可溯源的事实。`value=None` 表示数据缺失（不是 0）。"""
    value: Optional[float]
    display: str
    unit: str = ""
    coord: str = ""
    source_file: str = ""
    formula: Optional[str] = None
    note: str = ""
    source_kind: str = "raw"
    period: str = ""
    input_sources: List[Dict[str, Any]] = field(default_factory=list)
    core_fact_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FactSet:
    product: str
    month: str
    prev_month: str
    year_ago_month: str
    facts: Dict[str, Fact] = field(default_factory=dict)
    monthly: List[Dict[str, Any]] = field(default_factory=list)   # 近 6 个月趋势
    materials: List[Dict[str, Any]] = field(default_factory=list)  # 原材料明细
    mfg: List[Dict[str, Any]] = field(default_factory=list)        # 制造费用五类
    peer: List[Dict[str, Any]] = field(default_factory=list)       # 对标
    missing: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)             # 已知不可自洽点
    quality: List[Dict[str, Any]] = field(default_factory=list)
    period_kind: str = "monthly"
    period_label: str = ""
    input_periods: Tuple[str, ...] = ()
    topic_element: str = ""
    topic_rows: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["facts"] = {k: v.to_dict() for k, v in self.facts.items()}
        return d


# ---------------------------------------------------------------------------

def list_products() -> List[str]:
    rows = _read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv")
    seen: List[str] = []
    for r in rows:
        p = (r.get("产品名称") or "").strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def _shift_month(month: str, delta: int) -> Optional[str]:
    """`2026-05` ± delta 个月。**不跨出数据窗口**时返回，否则 None。"""
    y, m = int(month[:4]), int(month[5:7])
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def build(product: str, month: str, workspace_root: Optional[Path] = None) -> FactSet:
    """Read one selected workspace without modifying global module paths."""
    if workspace_root is not None:
        with data_workspace(workspace_root):
            return _build(product, month)
    return _build(product, month)


def _build(product: str, month: str) -> FactSet:
    """算出某产品某月的全部报告事实。"""
    m26 = _read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv")
    m25 = _read("csv/cost_data/中药一厂_成本汇总_2025年1-6月.csv")
    bud = _read("csv/cost_data/中药一厂_预算数据_2026年.csv")
    lab = _read("csv/cost_data/中药一厂_人工工时明细_2026年1-6月.csv")
    mfg = _read("csv/cost_data/中药一厂_制造费用明细_2026年1-6月.csv")
    mat = _read("csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv")
    p2 = _read("csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv")
    wx = _read("csv/market_data/药材市场价格行情_2026年上半年.csv")

    F26, F25, FB = ("中药一厂_成本汇总_2026年1-6月.csv",
                    "中药一厂_成本汇总_2025年1-6月.csv",
                    "中药一厂_预算数据_2026年.csv")
    FL, FM, FT = ("中药一厂_人工工时明细_2026年1-6月.csv",
                  "中药一厂_制造费用明细_2026年1-6月.csv",
                  "中药一厂_原材料消耗明细_2026年1-6月.csv")

    fs = FactSet(product=product, month=month,
                 prev_month=_shift_month(month, -1) or month,
                 year_ago_month="2025-" + month[5:])
    add = fs.facts.__setitem__
    if product not in UNIT_PER_BOX:
        fs.missing.append(f"未知产品: {product}")
        return fs

    cur = _pick(m26, 产品名称=product, 月份=month)
    prev = _pick(m26, 产品名称=product, 月份=fs.prev_month)
    yago = _pick(m25, 产品名称=product, 月份=fs.year_ago_month)
    budr = _pick(bud, 产品名称=product, 月份=month)

    # 实际有 9/10 月简报（一厂），但该简报只含单位成本 → 单独处理，见 brief
    if cur is None:
        fs.missing.append(f"2026 年 {month} 成本汇总缺失（该窗口 raw 无数据）")

    def raw(table, row, col, fname, tag) -> Optional[float]:
        if row is None:
            return None
        return _num(row.get(col))

    # ---- 2.1 核心指标一览 ----
    def three(col: str, label: str, table_cur, row_cur, table_prev, row_prev):
        c = raw(table_cur, row_cur, col, F26, f"{product}&{month}")
        p = raw(table_cur, row_prev, col, F26, f"{product}&{fs.prev_month}")
        key = col
        rng = f"{product}&{month}"
        if c is not None:
            add(key, Fact(c, _fmt(c, 0), "", _coord(F26, col, rng), F26))
        else:
            fs.missing.append(f"{label}（本月）")
        if p is not None:
            add("上月" + key, Fact(p, _fmt(p, 0), "", _coord(F26, col, f"{product}&{fs.prev_month}"), F26))
        d = _pct(c, p)
        if d is not None:
            add(key + "环比", Fact(d, f"{d:+.2f}%", "%", _coord(F26, col, rng), F26,
                                   formula=_pct_formula(c, p)))

    # 产量
    three("产量(盒)", "产量", m26, cur, m26, prev)
    # 单位成本
    c = raw(m26, cur, "单位成本(元/盒)", F26, "")
    p = raw(m26, prev, "单位成本(元/盒)", F26, "")
    ya = raw(m25, yago, "单位成本(元/盒)", F25, "")
    bv = raw(bud, budr, "预算单位成本(元/盒)", FB, "")
    if c is not None:
        # ⚠️ 覆写模式下（季度聚合）没有可指的 raw 列坐标——
        #    这时 `coord` 留空、改给公式，如实说明"这是期间聚合值"。
        #    硬编一个坐标等于**伪造溯源**，比留空更坏。
        add("本月单位成本", Fact(
            c, _fmt(c), "元/盒",
            "" if _ROW_CONTEXT.get()[0] is not None else _coord(F26, "单位成本(元/盒)", f"{product}&{month}"),
            F26,
            formula="总成本 ÷ 总产量（期间聚合）" if _ROW_CONTEXT.get()[0] is not None else None))
    if p is not None:
        add("上月单位成本", Fact(p, _fmt(p), "元/盒",
            _coord(F26, "单位成本(元/盒)", f"{product}&{fs.prev_month}"), F26))
    if ya is not None:
        add("去年单位成本", Fact(ya, _fmt(ya), "元/盒",
            _coord(F25, "单位成本(元/盒)", f"{product}&{fs.year_ago_month}"), F25))
    if bv is not None:
        add("预算单位成本", Fact(bv, _fmt(bv), "元/盒",
            _coord(FB, "预算单位成本(元/盒)", f"{product}&{month}"), FB))
    for key, num, den in (("单位成本环比", c, p), ("单位成本同比", c, ya),
                          ("单位成本预算偏差", c, bv)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%",
                          _coord(F26, "单位成本(元/盒)", f"{product}&{month}"),
                          F26, formula=_pct_formula(num, den)))

    # 总成本
    tc = raw(m26, cur, "总成本(元)", F26, "")
    tp = raw(m26, prev, "总成本(元)", F26, "")
    ty = raw(m25, yago, "总成本(元)", F25, "")
    tb = raw(bud, budr, "预算总成本(元)", FB, "")
    for key, val, unit, tbl, tag in (
            ("本月总成本", tc, "元", F26, f"{product}&{month}"),
            ("上月总成本", tp, "元", F26, f"{product}&{fs.prev_month}"),
            ("去年总成本", ty, "元", F25, f"{product}&{fs.year_ago_month}"),
            ("预算总成本", tb, "元", FB, f"{product}&{month}")):
        if val is not None:
            add(key, Fact(val, _fmt(val, 0), unit,
                          _coord(tbl, "总成本(元)" if "预算" not in key else "预算总成本(元)", tag), tbl))
    for key, num, den in (("总成本环比", tc, tp), ("总成本同比", tc, ty),
                          ("总成本预算偏差", tc, tb)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%", _coord(F26, "总成本(元)", f"{product}&{month}"),
                          F26, formula=_pct_formula(num, den)))

    # 产量同比/预算偏差（产量环比已由 three() 产出）
    pv = raw(m26, cur, "产量(盒)", F26, "")
    pp = raw(m26, prev, "产量(盒)", F26, "")
    py = raw(m25, yago, "产量(盒)", F25, "")
    pb = raw(bud, budr, "预算产量(盒)", FB, "")
    for key, val, tbl, tag in (("去年同月产量", py, F25, f"{product}&{fs.year_ago_month}"),
                               ("预算产量", pb, FB, f"{product}&{month}")):
        if val is not None:
            add(key, Fact(val, _fmt(val, 0), "盒", _coord(tbl, "产量(盒)" if tbl == F25 else "预算产量(盒)", tag), tbl))
    for key, num, den in (("产量同比", pv, py), ("产量预算偏差", pv, pb)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%", _coord(F26, "产量(盒)", f"{product}&{month}"),
                          F26, formula=_pct_formula(num, den)))

    # 三要素（本月/上月/环比/预算/偏差）
    for col, tag, name in (("直接材料(元/盒)", "材料", "材料"), ("直接人工(元/盒)", "人工", "人工"),
                           ("制造费用(元/盒)", "制造", "制造费用")):
        cc = raw(m26, cur, col, F26, "")
        cp = raw(m26, prev, col, F26, "")
        cb = raw(bud, budr, "预算" + col, FB, "")
        if cc is not None:
            add(f"本月{tag}成本", Fact(cc, _fmt(cc), "元/盒",
                _coord(F26, col, f"{product}&{month}"), F26))
        if cp is not None:
            add(f"上月{tag}成本", Fact(cp, _fmt(cp), "元/盒",
                _coord(F26, col, f"{product}&{fs.prev_month}"), F26))
        if cb is not None:
            add(f"预算{tag}成本", Fact(cb, _fmt(cb), "元/盒",
                _coord(FB, "预算" + col, f"{product}&{month}"), FB))
        v = _pct(cc, cp)
        if v is not None:
            add(f"{name}成本环比" if name != "制造费用" else "制造费用环比",
                Fact(v, f"{v:+.2f}%", "%", _coord(F26, col, f"{product}&{month}"), F26,
                     formula=_pct_formula(cc, cp)))
        v = _pct(cc, cb)
        if v is not None:
            add(f"{name}预算偏差", Fact(v, f"{v:+.2f}%", "%",
                _coord(F26, col, f"{product}&{month}"), F26,
                formula=_pct_formula(cc, cb)))

    # ---- 2.2 成本结构（占比 + 贡献度）----
    # 贡献度 = 该要素的「单位成本变动额」÷ 「单位成本变动总额」
    #   [成本异动登记册] 已核验：三项贡献度之和必须 = 100%
    dc = raw(m26, cur, "直接材料(元/盒)", F26, "")
    dp = raw(m26, prev, "直接材料(元/盒)", F26, "")
    lc = raw(m26, cur, "直接人工(元/盒)", F26, "")
    lp = raw(m26, prev, "直接人工(元/盒)", F26, "")
    mc = raw(m26, cur, "制造费用(元/盒)", F26, "")
    mp = raw(m26, prev, "制造费用(元/盒)", F26, "")
    if None not in (dc, lc, mc):
        tot = dc + lc + mc
        for tag, val in (("材料", dc), ("人工", lc), ("制造费用", mc)):
            add(f"{tag}金额", Fact(val, _fmt(val), "元/盒",
                _coord(F26, f"直接{'材料' if tag=='材料' else '人工' if tag=='人工' else ''}(元/盒)"
                       if tag != "制造费用" else "制造费用(元/盒)", f"{product}&{month}"), F26))
            if tot:
                add(f"{tag}占比", Fact(val / tot * 100, f"{val/tot*100:.2f}%", "%",
                    _coord(F26, "单位成本(元/盒)", f"{product}&{month}"), F26,
                    formula=f"{_formula_number(val)} ÷ {_formula_number(tot)} × 100"))
        delta_tot = (dc + lc + mc) - ((dp or 0) + (lp or 0) + (mp or 0)) if None not in (dp, lp, mp) else None
        if delta_tot:
            for tag, c0, p0 in (("材料", dc, dp), ("人工", lc, lp), ("制造费用", mc, mp)):
                if c0 is None or p0 is None:
                    continue
                d = c0 - p0
                add(f"{tag}贡献度", Fact(d / delta_tot * 100, f"{d/delta_tot*100:.2f}%", "%",
                    _coord(F26, "单位成本(元/盒)", f"{product}&{month}"), F26,
                    formula=f"({_formula_number(c0)} − {_formula_number(p0)}) ÷ {_formula_number(delta_tot)} × 100"))

    # ---- 3.2 直接人工四指标 ----
    lc_row = _pick(lab, 产品名称=product, 月份=month)
    lp_row = _pick(lab, 产品名称=product, 月份=fs.prev_month)
    if lc_row and lp_row and pv:
        def lab_calc(row, tag, rng):
            tot_l = _num(row.get("直接人工总额(元)"))
            hours = _num(row.get("总工时(小时)"))
            people = _num(row.get("生产人数(人)"))
            days = _num(row.get("工作天数(天)"))
            prod = _num(row.get("产量(盒)"))
            if None in (tot_l, hours, prod) or prod == 0:
                return
            add(f"人工单位成本{tag}", Fact(tot_l / prod, _fmt(tot_l / prod), "元/盒",
                _coord(FL, "直接人工总额(元),产量(盒)", f"{product}&{rng}"), FL,
                formula=f"{_formula_number(tot_l)} ÷ {_formula_number(prod)}"))
            if hours:
                v = hours / prod * 10000
                add(f"本月工时" if tag == "" else f"上月工时",
                    Fact(v, _fmt(v, 2), "h/万盒",
                         _coord(FL, "总工时(小时),产量(盒)", f"{product}&{rng}"), FL,
                         formula=f"{_formula_number(hours)} ÷ {_formula_number(prod)} × 10000"))
                add(f"本月时薪" if tag == "" else f"上月时薪",
                    Fact(tot_l / hours, _fmt(tot_l / hours), "元/h",
                         _coord(FL, "直接人工总额(元),总工时(小时)", f"{product}&{rng}"), FL,
                         formula=f"{_formula_number(tot_l)} ÷ {_formula_number(hours)}"))
            person_days = (_num(row.get("__person_days")) if "__person_days" in row
                           else people * days if people is not None and days is not None else None)
            if person_days:
                v = prod / person_days
                add(f"本月效率" if tag == "" else f"上月效率",
                    Fact(v, _fmt(v, 2), "盒/人·日",
                         _coord(FL, "产量(盒),生产人数(人),工作天数(天)", f"{product}&{rng}"), FL,
                         formula=(f"{_formula_number(prod)} ÷ {_formula_number(person_days)}"
                                  if "__person_days" in row else
                                  f"{_formula_number(prod)} ÷ ({_formula_number(people)} × {_formula_number(days)})")))
        lab_calc(lc_row, "", month)
        lab_calc(lp_row, "上月", fs.prev_month)
        for k in ("工时", "时薪", "效率"):
            a, b = fs.facts.get(f"本月{k}"), fs.facts.get(f"上月{k}")
            if a and b:
                v = _pct(a.value, b.value)
                if v is not None:
                    add(f"{k}环比", Fact(v, f"{v:+.2f}%", "%", a.coord, FL,
                        formula=_pct_formula(a.value, b.value)))

    # ---- 3.3 制造费用五类 ----
    for cat, tname in MFG_TEMPLATE_NAMES.items():
        r_c = _pick(mfg, 产品名称=product, 月份=month, 费用类别=cat)
        r_p = _pick(mfg, 产品名称=product, 月份=fs.prev_month, 费用类别=cat)
        if r_c:
            v = _num(r_c.get("单位费用(元/盒)"))
            if v is not None:
                add(f"本月{tname}", Fact(v, _fmt(v), "元/盒",
                    _coord(FM, "单位费用(元/盒)", f"{product}&{cat}&{month}"), FM))
        if r_p:
            v = _num(r_p.get("单位费用(元/盒)"))
            if v is not None:
                add(f"上月{tname}", Fact(v, _fmt(v), "元/盒",
                    _coord(FM, "单位费用(元/盒)", f"{product}&{cat}&{fs.prev_month}"), FM))
        a, b = fs.facts.get(f"本月{tname}"), fs.facts.get(f"上月{tname}")
        if a and b:
            v = _pct(a.value, b.value)
            if v is not None:
                add(f"{tname}环比", Fact(v, f"{v:+.2f}%", "%", a.coord, FM,
                    formula=_pct_formula(a.value, b.value)))
        fs.mfg.append({"类别": tname, "csv类别": cat,
                       "本月": a.display if a else "—",
                       "上月": b.display if b else "—",
                       "环比": fs.facts.get(f"{tname}环比").display if fs.facts.get(f"{tname}环比") else "—"})
    # 合计校验：五类之和应与成本汇总的「制造费用(元/盒)」一致
    if mc is not None:
        s = sum(fs.facts[f"本月{n}"].value for n in MFG_TEMPLATE_NAMES.values()
                if fs.facts.get(f"本月{n}") and fs.facts[f"本月{n}"].value is not None)
        if abs(s - mc) > 0.02:
            fs.conflicts.append(
                f"制造费用五类之和 {_fmt(s)} ≠ 成本汇总的制造费用 {_fmt(mc)}"
                f"（差 {_fmt(s-mc)}）——需人工判读是否为舍入或口径差异")

    # ---- 3.1 原材料明细 ----
    mats_c = [r for r in mat if (r.get("产品名称") or "").strip() == product
              and (r.get("月份") or "").strip() == month]
    for r in mats_c:
        name = (r.get("原材料名称") or "").strip()
        cu = _num(r.get("单位消耗成本(元/盒)"))
        rp = _pick(mat, 产品名称=product, 月份=fs.prev_month, 原材料名称=name)
        pu = _num(rp.get("单位消耗成本(元/盒)")) if rp else None
        d = _pct(cu, pu)
        fs.materials.append({
            "原材料名称": name,
            "本月": _fmt(cu), "上月": _fmt(pu),
            "环比": f"{d:+.2f}%" if d is not None else "—",
            "占比": (r.get("占总材料成本比例") or "—").strip(),
            "单价来源": f"原材料消耗明细（单位消耗成本，非采购单价）：{FT}",
            "坐标": _coord(FT, "单位消耗成本(元/盒)", f"{product}&{name}&{month}"),
        })

    # ---- 4.1 近 6 个月趋势 ----
    for r in m26:
        if (r.get("产品名称") or "").strip() != product:
            continue
        if (r.get("月份") or "").strip() > month:
            continue
        fs.monthly.append({
            "月份": r["月份"],
            "产量": (r.get("产量(盒)") or "").strip(),
            "单位材料": _fmt(_num(r.get("直接材料(元/盒)"))),
            "单位人工": _fmt(_num(r.get("直接人工(元/盒)"))),
            "单位制造费用": _fmt(_num(r.get("制造费用(元/盒)"))),
            "单位成本": _fmt(_num(r.get("单位成本(元/盒)"))),
        })
    fs.monthly.sort(key=lambda x: x["月份"])
    for i, row in enumerate(fs.monthly):
        if i == 0:
            row["环比"] = "—"
            continue
        prev_v = _num(fs.monthly[i - 1]["单位成本"])
        cur_v = _num(row["单位成本"])
        d = _pct(cur_v, prev_v)
        row["环比"] = f"{d:+.2f}%" if d is not None else "—"

    # ---- 4.3 原材料价格跟踪 ----
    market_month = int(month[5:7])
    market_col = f"{market_month}月价格"
    fs.market = []  # type: ignore[attr-defined]
    for r in wx:
        nm = (r.get("药材名称") or "").strip()
        used = next((m["原材料名称"] for m in fs.materials if m["原材料名称"] == nm), None)
        jan, current = _num(r.get("1月价格")), _num(r.get(market_col))
        d = _pct(current, jan)
        fs.market.append({  # type: ignore[attr-defined]
            "原材料": nm, "单位": (r.get("单位") or "").strip(),
            "年初价": _fmt(jan, 1), "本期价": _fmt(current, 1),
            "价格月份": month,
            "6月价": _fmt(current, 1) if market_month == 6 else "—",
            "涨幅": format_number(d, signed=True) + "%" if d is not None else "—",
            "市场趋势": ((r.get("趋势分析") or "").strip() if market_month == 6
                         else "原资料为上半年总体趋势，非本月结论"),
            "坐标": _coord("药材市场价格行情_2026年上半年.csv", market_col, nm),
            "用于本产品": "✓" if used else "",
        })

    # ---- 5 对标（一厂 vs 二厂）----
    p2c = _pick(p2, 产品名称=product, 月份=month)
    for dim, col in (("单位成本", "单位成本(元/盒)"), ("直接材料", "直接材料(元/盒)"),
                     ("直接人工", "直接人工(元/盒)"), ("制造费用", "制造费用(元/盒)")):
        a = raw(m26, cur, col, F26, "")
        b = raw(p2, p2c, col, "中药二厂_成本汇总_2026年1-6月.csv", "")
        if a is None or b is None:
            continue
        comparison = compare_cost(cur.get(col), p2c.get(col))
        fs.peer.append({
            "维度": dim, "中药一厂": _fmt(a), "中药二厂": _fmt(b),
            "差异": format_number(comparison["difference"], signed=True),
            "差异金额": comparison["difference"],
            "差异率": (format_number(comparison["rate"], signed=True) + "%"
                       if comparison["rate"] is not None else "—"),
            "方向": comparison["direction"],
            "口径": DIFF_NOTE,
        })

    # ---- 已知不可自洽点：**从数据文件读**，不写死在代码里 ----
    # ⚠️ 这两句原本硬编码在此处。它们描述的是**具体某批数据**的口径争议
    #    （官方宣称的涨幅、工艺定额与实际值的差异），属于"关于数据的事实"，
    #    本该跟数据一起走。
    #    写死在这里的后果：换一套数据（如自造的样例工作区）后，
    #    报告仍会印出针对**另一批数据**的说法——读者会看到一句
    #    与眼前数字毫无关系的断言。而且它是"看起来对"的错，校验器拦不住。
    fs.conflicts.extend(_data_conflicts())

    _apply_core(fs, product, month)
    return fs


def _apply_core(fs: FactSet, product: str, month: str) -> None:
    """Project neutral core values into the existing pharma presentation keys.

    The original plugin determines which legacy keys exist (including January's
    intentionally absent previous-month fields). Generic values that are exposed
    to callers come from the neutral engine, without rounding the stored value.
    """
    from cost_core import engine as core_engine
    from cost_core.models import SourceRef
    from .adapter import load_snapshot

    mapping, input_periods = _ROW_CONTEXT.get()
    request = load_snapshot(product, month, row_provider=_read,
                            synthetic_files=tuple(mapping) if mapping is not None else (),
                            input_periods=input_periods)
    result = core_engine.analyze(request)
    metric_keys = {
        "产量(盒)": "quantity", "产量(盒)环比": "quantity.mom_pct",
        "产量同比": "quantity.yoy_pct", "产量预算偏差": "quantity.budget_pct",
        "本月单位成本": "unit_cost", "单位成本环比": "unit_cost.mom_pct",
        "单位成本同比": "unit_cost.yoy_pct", "单位成本预算偏差": "unit_cost.budget_pct",
        "本月总成本": "total_cost", "总成本环比": "total_cost.mom_pct",
        "总成本同比": "total_cost.yoy_pct", "总成本预算偏差": "total_cost.budget_pct",
    }
    for category, short, long in (("materials", "材料", "材料"),
                                   ("labor", "人工", "人工"),
                                   ("manufacturing_overhead", "制造", "制造费用")):
        prefix = f"category.{category}"
        metric_keys[f"本月{short}成本"] = prefix + ".unit_cost"
        metric_keys[f"{long}金额"] = prefix + ".unit_cost"
        metric_keys[f"{long}占比"] = prefix + ".share_pct"
        metric_keys[f"{long}贡献度"] = prefix + ".contribution_pct"
        metric_keys[f"{long}成本环比" if long != "制造费用" else "制造费用环比"] = prefix + ".mom_pct"
        metric_keys[f"{long}预算偏差"] = prefix + ".budget_pct"

    def attach(fact: Fact, refs, *, source_kind: str = "", period: str = month):
        fact.input_sources = [asdict(ref) for ref in refs]
        fact.source_kind = ("derived" if any(ref.kind == "derived" for ref in refs)
                            else "synthetic" if any(ref.kind == "synthetic" for ref in refs)
                            else "inferred" if any(ref.kind == "inferred" for ref in refs)
                            else source_kind or (refs[0].kind if refs else "missing"))
        fact.period = period
        if fact.source_kind in {"synthetic", "derived"}:
            detail = "期间合成观测；月份为兼容连接标签，原始inputSources及派生说明见input_sources。"
            fact.note = (fact.note + " " + detail).strip()

    # The core replaces generic legacy float values with Decimal results below.
    # Formula operands must come from that same request, not the earlier float
    # calculation: even an unrounded float can lose a tiny category change.
    operand_attributes = {"quantity": "quantity", "unit_cost": "reported_unit_cost",
                          "total_cost": "reported_total"}
    formula_baselines = {"mom_pct": request.previous, "yoy_pct": request.year_ago,
                         "budget_pct": request.budget}

    def operand(observation, prefix):
        if observation is None:
            return None
        if prefix in operand_attributes:
            return getattr(observation, operand_attributes[prefix])
        return observation.unit_costs[prefix.removeprefix("category.")]

    def category_total(observation):
        if observation is None or any(value is None for value in observation.unit_costs.values()):
            return None
        return sum(observation.unit_costs.values(), Decimal(0))

    current_components = category_total(request.current)
    previous_components = category_total(request.previous)

    def core_numeric_formula(key):
        if "." not in key:
            return None
        prefix, suffix = key.rsplit(".", 1)
        if suffix in formula_baselines:
            current = operand(request.current, prefix)
            prior = operand(formula_baselines[suffix], prefix)
            if current is not None and prior not in (None, 0):
                return _pct_formula(current, prior)
        elif suffix == "share_pct":
            current = operand(request.current, prefix)
            if current is not None and current_components not in (None, 0):
                return f"{_formula_number(current)} ÷ {_formula_number(current_components)} × 100"
        elif suffix == "contribution_pct":
            current, prior = operand(request.current, prefix), operand(request.previous, prefix)
            if None not in (current, prior, current_components, previous_components):
                denominator = current_components - previous_components
                if denominator:
                    return (f"({_formula_number(current)} − {_formula_number(prior)})"
                            f" ÷ {_formula_number(denominator)} × 100")
        return None

    for old_key, key in metric_keys.items():
        if old_key not in fs.facts:
            continue
        metric = result.metrics[key]
        fact = fs.facts[old_key]
        fact.value = float(metric.value) if metric.value is not None else None
        places = 0 if key in {"quantity", "total_cost"} else 2
        signed = key.endswith(("mom_pct", "yoy_pct", "budget_pct"))
        fact.display = format_number(metric.value, places=places, signed=signed)
        if metric.unit == "%" and metric.value is not None:
            fact.display += "%"
        fact.core_fact_id = metric.fact_id
        precise_formula = core_numeric_formula(key)
        if precise_formula is not None:
            fact.formula = precise_formula
        elif metric.formula and not fact.formula:
            fact.formula = metric.formula
        attach(fact, metric.source_refs)

    # Prior-period and budget base values are source observations, not separately
    # calculated indicators. Preserve their original precision and source identity.
    bases = [
        ("上月产量(盒)", request.previous, "quantity", "quantity", 0),
        ("去年同月产量", request.year_ago, "quantity", "quantity", 0),
        ("预算产量", request.budget, "quantity", "quantity", 0),
        ("上月单位成本", request.previous, "reported_unit_cost", "unit_cost", 2),
        ("去年单位成本", request.year_ago, "reported_unit_cost", "unit_cost", 2),
        ("预算单位成本", request.budget, "reported_unit_cost", "unit_cost", 2),
        ("上月总成本", request.previous, "reported_total", "total_cost", 0),
        ("去年总成本", request.year_ago, "reported_total", "total_cost", 0),
        ("预算总成本", request.budget, "reported_total", "total_cost", 0),
    ]
    for category, label in (("materials", "材料"), ("labor", "人工"), ("manufacturing_overhead", "制造")):
        for role, observation in (("上月", request.previous), ("预算", request.budget)):
            bases.append((f"{role}{label}成本", observation, "category:" + category,
                          f"category.{category}.unit_cost", 2))
    for old_key, observation, attribute, source_key, places in bases:
        if old_key not in fs.facts or observation is None:
            continue
        value = (observation.unit_costs[attribute.split(":", 1)[1]]
                 if attribute.startswith("category:") else getattr(observation, attribute))
        fact = fs.facts[old_key]
        fact.value = float(value) if value is not None else None
        fact.display = format_number(value, places=places)
        attach(fact, observation.sources.get(source_key, ()),
               source_kind=observation.source_kind, period=observation.period)

    labels = {"unit_cost": "单位成本", "category.materials.unit_cost": "直接材料",
              "category.labor.unit_cost": "直接人工", "category.manufacturing_overhead.unit_cost": "制造费用"}
    by_label = {labels[row.metric_id]: row for row in result.comparison}
    for old_row in fs.peer:
        row = by_label.get(old_row["维度"])
        if row is None:
            continue
        old_row["中药一厂"] = format_number(row.self_value)
        old_row["中药二厂"] = format_number(row.peer_value)
        old_row["差异"] = format_number(row.difference, signed=True)
        old_row["差异金额"] = float(format_number(row.difference)) if row.difference is not None else None
        old_row["差异率"] = format_number(row.rate, signed=True) + "%" if row.rate is not None else "—"
        old_row["方向"] = LEGACY_DIRECTIONS[row.direction]
        old_row["source_kind"] = ("derived" if any(ref.kind == "derived" for ref in row.source_refs)
                                  else "synthetic" if any(ref.kind == "synthetic" for ref in row.source_refs)
                                  else "inferred" if any(ref.kind == "inferred" for ref in row.source_refs) else "raw")
        old_row["period"] = month
        old_row["input_sources"] = [asdict(ref) for ref in row.source_refs]
    # Pharma-only detail calculations may also consume quarter overrides. Keep
    # their legacy coordinates for compatibility, but never label them raw facts.
    synthetic_by_name = {Path(name).name: name for name in (mapping or {})}
    for fact_name, fact in fs.facts.items():
        filename = synthetic_by_name.get(Path(fact.source_file).name)
        if filename is None or fact.input_sources:
            continue
        label_match = re.findall(r"\d{4}-\d{2}", fact.coord)
        alias = label_match[-1] if label_match else month
        raw_refs = []
        periods = set()
        fields = fact.coord.split(":", 2)[1].split(",") if ":" in fact.coord else []
        for row in (mapping or {}).get(filename, []):
            relevant_aliases = {alias, fs.prev_month} if fact_name.endswith("环比") else {alias}
            if row.get("产品名称") != product or row.get("月份") not in relevant_aliases:
                continue
            category = row.get("费用类别") or row.get("原材料名称")
            if category and category not in fact.coord:
                continue
            periods.update(row.get("__input_periods", ()))
            by_field = row.get("__detail_input_refs", {})
            for field_name in fields:
                raw_refs.extend(by_field.get(field_name, ()))
            # Any per-unit/efficiency calculation also depends on its denominator.
            if raw_refs:
                raw_refs.extend(by_field.get("产量(盒)", ()))
        ref = SourceRef(dataset_id=request.context.dataset_id,
                        source_uri=f"derived:pharma-detail:{product}@{alias}", field=fact_name,
                        selector=f"产品名称={product}&synthetic_join_label={alias}",
                        kind="derived",
                        formula=("期间聚合器注入的药企明细合成行；旧坐标仅作兼容连接标签；输入月份="
                                 + ",".join(sorted(periods)) if periods else
                                 "期间聚合器注入的药企明细合成行；输入月份未提供，不冒充原始坐标。"))
        unique_refs = tuple(dict.fromkeys(raw_refs))
        attach(fact, (*unique_refs, ref), period=alias)
    fs.quality = [{"code": issue.code, "message": issue.message, "metric_id": issue.metric_id,
                   "deviation": str(issue.deviation) if issue.deviation is not None else None,
                   "input_sources": [asdict(ref) for ref in issue.source_refs]}
                  for issue in result.quality]


# 口径说明文件：缺失即为空，不报错（老工作区没有这个文件也要能跑）
_CONFLICTS_FILE = RAW / "meta/口径说明.json"


def _data_conflicts() -> List[str]:
    """
    读 `raw/meta/口径说明.json` 里的 `已知不可自洽点`。

    结构：
        {"已知不可自洽点": ["...", "..."]}
    文件不在 / 解析失败 / 键缺失 → 返回空列表（**静默降级**）。
    这里不该因为"没有口径说明"而让取数失败——它只是补充信息。
    """
    path = (current_workspace() / "raw/meta/口径说明.json"
            if _DATA_WORKSPACE.get() is not None else _CONFLICTS_FILE)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get("已知不可自洽点") or []
    return [str(x) for x in items]


# ---------------------------------------------------------------------------

def _print(fs: FactSet) -> None:
    print("=" * 74)
    print(f"报告事实  {fs.product}  {fs.month}  （对比 {fs.prev_month} / {fs.year_ago_month}）")
    print("=" * 74)
    for k, v in fs.facts.items():
        f = f"   = {v.formula}" if v.formula else ""
        print(f"  {k:<22} {v.display:>12}{(' ' + v.unit) if v.unit else ''}{f}")
        if v.coord:
            print(f"  {'':<22} ↳ {v.coord}")
    if fs.missing:
        print("\n⚠️  数据缺失（**不是 0**，不得填零）:")
        for m in fs.missing:
            print(f"   · {m}")
    if fs.conflicts:
        print("\n⚠️  已知不可自洽点（写报告时须回避或标注）:")
        for c in fs.conflicts:
            print(f"   · {c}")


def main() -> int:
    ap = argparse.ArgumentParser(description="报告事实引擎（确定性取数）")
    ap.add_argument("--product", help="产品名")
    ap.add_argument("--month", default="2026-05", help="分析月份 YYYY-MM")
    ap.add_argument("--list-products", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", help="写入 JSON 文件")
    a = ap.parse_args()

    if a.list_products:
        for p in list_products():
            print(p)
        return 0
    if not a.product:
        ap.print_help()
        return 1

    fs = build(a.product, a.month)
    d = fs.to_dict()
    if a.out:
        Path(a.out).write_text(json.dumps(d, ensure_ascii=False, indent=2),
                               encoding="utf-8", newline="\n")
        print(f"已写出 {a.out}（{len(fs.facts)} 条事实）")
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    elif not a.out:
        _print(fs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
