# -*- coding: utf-8 -*-
"""Legacy compatibility implementation; original source materials are not included."""


































from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from console_io import ensure_utf8_stdout  # noqa: E402
from errors import ConfigError  # noqa: E402
from .grounding import (context_for, evidence_records, fact_records,
                        render_grounded_field, validate_qualitative)  # noqa: E402
from cost_conventions import DIFF_NOTE, as_decimal, format_number  # noqa: E402
import report_facts as RF  # noqa: E402
from cost_core import reporting as CR  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parents[2]
WIKI_ROOT = HERE.parent
TEMPLATE = WIKI_ROOT / "references/templates/月度成本分析报告模板.md"
OUT_DIR = WIKI_ROOT / "reports"


# ⚠️ 季度/专题**必须用独立模板**，不能套月度那份：
#    月度模板里有 22 处「本月」，季度报告直接套用会把季度合计
#    标成"本月"——**标签撒谎**，数字对但读起来是错的。
TEMPLATES = {
    "monthly": TEMPLATE,
    "quarterly": WIKI_ROOT / "references/templates/季度成本分析报告模板.md",
    "topic": WIKI_ROOT / "references/templates/专题分析报告模板.md",
}
THEME_ZH = {"monthly": "月度成本分析", "quarterly": "季度成本分析",
            "topic": "专题分析"}

PRODUCT_CODE = {"银黄口服液": "YH", "板蓝根颗粒": "BLG", "六味地黄胶囊": "LWD"}
PRODUCT_SPEC = {"银黄口服液": "10ml×10支/盒", "板蓝根颗粒": "10g×20袋/盒",
                "六味地黄胶囊": "0.3g×60粒/盒"}


THRESHOLD_PCT = 10.0




ELEMENT_FACTS: List[Tuple[str, str]] = [
    ("直接材料", "材料成本环比"),
    ("直接人工", "人工成本环比"),
    ("制造费用", "制造费用环比"),
]


def evaluate_threshold(fs: RF.FactSet) -> Dict[str, Any]:
    """Adapt the neutral threshold result to the existing pharma labels/API."""
    return CR.evaluate_threshold(
        {label: fs.facts.get(key) for label, key in ELEMENT_FACTS},
        THRESHOLD_PCT, components=[label for label, _ in ELEMENT_FACTS],
        comparator="gte")


def threshold_verdict(th: Dict[str, Any]) -> str:
    t = th["threshold"]
    unknown = th.get("unknown", [])
    if th["breaches"]:
        detail = "、".join(f"{n} {v}" for n, v in th["breaches"])
        result = (f"已有 {len(th['breaches'])} 项成本要素环比变动超过 ±{t:.0f}%：{detail}。"
                  f"按赛题要求生成重点分析（见下方）。")
        return result + ("另有缺失要素待核查：" + "、".join(unknown) + "。" if unknown else "")
    if unknown or not th["within"]:
        detail = "、".join(unknown) or "全部成本要素"
        return f"成本要素环比资料不完整（{detail}），阈值判定未完成；不得据此认定全部在阈值内。"
    detail = "、".join(f"{n} {v}" for n, v in th["within"])
    return (f"本月各成本要素环比变动均在 ±{t:.0f}% 阈值以内（{detail}），"
            f"无成本要素触发告警。")


# 需要 LLM 写作的字段（其余全部由脚本填）
LLM_SCALAR_FIELDS = {
    "材料成本归因分析文本": "120-200 字。按贡献度排序说明材料成本变动的主因，点到具体药材。",
    "成本异常排查分析": "150-250 字。结合近 6 个月趋势指出异常月份与成因。",
    "差异结构拆解分析": "120-200 字。拆解一厂与二厂的成本结构差异出在哪一层。",
    "差异归因分析文本": "100-160 字。解释差异的可能成因，区分管理与采购因素。",
    "本月亮点": "60-100 字。成本管理上值得肯定的地方（数据支撑）。",
    "需关注问题": "80-140 字。最需要管理层关注的问题，按严重度排序。",
}
# 专题分析专用的 LLM 字段。**只在 topic 主题下请求**——
# 月度/季度报告没有这些占位符，白写会浪费 token 且污染 analysis。
LLM_TOPIC_FIELDS = {
    "专题归因分析文本": "120-200 字。围绕该成本要素的波动，按驱动因素排序说明成因，"
                        "点到具体月份与金额；不得引入未经计算的因果断言。",
    "专题对标分析文本": "100-160 字。结合一厂与二厂在该要素上的差异，"
                        "说明可能的管理或采购成因。",
    "专题风险分析": "100-160 字。给出该要素的后续风险提示与可核查的观察指标。",
}

LLM_MFG_NOTES = ["折旧变动说明", "动力变动说明", "间接人工变动说明",
                 "检验变动说明", "其他变动说明"]

MAX_TOTAL_LLM_CHARS = 2400   # 防过度回答（本库实测：简单问题被答成 12,565 字）


# ---------------------------------------------------------------------------
# 一、确定性填充
# ---------------------------------------------------------------------------

def _fact_display(fs: RF.FactSet, name: str) -> Optional[str]:
    f = fs.facts.get(name)
    return f.display if f is not None else None


def _check_unique(pairs: List[Tuple[str, str]]) -> None:
    CR.ensure_unique_bindings(pairs)


def render_materials_table(fs: RF.FactSet) -> str:
    rows = []
    for i, m in enumerate(fs.materials, 1):
        judge = ""
        try:
            d = float(m["环比"].rstrip("%"))
            judge = "↑ 上涨" if d > 0 else ("↓ 下降" if d < 0 else "→ 持平")
        except (ValueError, AttributeError):
            judge = "—"
        rows.append(f"| {i} | {m['原材料名称']} | {m['本月']} | {m['上月']} | "
                    f"{m['环比']} | {judge} |")
    return "\n".join(rows) if rows else "| — | （无原材料明细数据） | — | — | — | — |"


def render_trend_table(fs: RF.FactSet) -> str:
    rows = []
    for r in fs.monthly:
        rows.append(f"| {r['月份']} | {r['产量']} | {r['单位材料']} | {r['单位人工']} | "
                    f"{r['单位制造费用']} | {r['单位成本']} | {r['环比']} |")
    return "\n".join(rows) if rows else "| — | — | — | — | — | — | — |"


def render_market_table(fs: RF.FactSet) -> str:
    rows = []
    for m in getattr(fs, "market", []):
        impact = "涉及本产品原料；采购传导需核查" if m["用于本产品"] else "—"
        rows.append(f"| {m['原材料']}（{m.get('单位', '见来源')}） | {m['年初价']} | {m.get('本期价', '—')} | {m['涨幅']} | "
                    f"{m['市场趋势']} | {impact} |")
    return "\n".join(rows) if rows else "| — | — | — | — | — | — |"


def render_peer_table(fs: RF.FactSet) -> str:
    rows = []
    for p in fs.peer:
        rows.append(f"| {p['维度']} | {p['一厂'] if '一厂' in p else p['中药一厂']} | "
                    f"{p['中药二厂']} | {p['差异']} | {p['差异率']} | {p['方向']} |")
    return "\n".join(rows) if rows else "| — | — | — | — | — | — |"


def render_advice_table(advice: List[Dict[str, str]]) -> str:
    rows = []
    for i, a in enumerate(advice, 1):
        rows.append(f"| {i} | {a.get('建议事项','—')} | {a.get('责任部门','—')} | "
                    f"{display_priority(a.get('优先级','中'))} | {a.get('预期效果','—')} | "
                    f"{a.get('建议完成时间','—')} |")
    return "\n".join(rows) if rows else "| — | （无建议） | — | — | — | — |"


# 报告里印**中文**（给人看），RPA 只收小写英文（接口约束）。
# 转换在提交边界做（`rpa_client.to_rpa_priority`），报告本身保持中文可读。
PRIORITY_ZH = {"高": "high", "中": "medium", "低": "low",
               "high": "high", "medium": "medium", "low": "low"}


def display_priority(v: Any) -> str:
    """任意写法 → 报告里显示的中文「高/中/低」。"""
    s = str(v).strip()
    rpa = PRIORITY_ZH.get(s, PRIORITY_ZH.get(s.lower(), "medium"))
    return {"high": "高", "medium": "中", "low": "低"}[rpa]


def render_task_table(advice: List[Dict[str, str]], month: str,
                      product: str) -> str:
    """
    6.4 整改任务清单。**字段与 RPA 接口一一对应**
    （`报告模板契约.md` §6.1）：任务编号→task_id、优先级→priority 等。

    机械部分归脚本：编号、截止时间。
    ⚠️ RPA 约束：`priority` 只能小写 high/medium/low；`task_id` 必须唯一。

    ⚠️ 编号**必须含产品码**：曾用 `TASK-<年月>-<序号>`，而编号里没有产品，
       于是同月不同产品撞同一个编号（实测银黄口服液与六味地黄胶囊都产出
       `TASK-202605-001`，任务内容却完全不同）。下游按 task_id 判唯一，
       第二个产品的任务会被判"已存在"而**根本没发出去**。
       现改为 `TASK-<产品码>-<年月>-<序号>`，与报告编号 RPT-{ym}-{code}-001 同构。
    """
    rows = []
    code = PRODUCT_CODE.get(product, "XX")
    ym = month.replace("-", "")
    for i, a in enumerate(advice, 1):
        pr = display_priority(a.get("优先级", "中"))
        rows.append(f"| TASK-{code}-{ym}-{i:03d} | {a.get('建议事项','—')} | "
                    f"{a.get('责任部门','—')} | {pr} | 月度成本分析报告 | "
                    f"{a.get('建议完成时间','—')} |")
    return "\n".join(rows) if rows else "| — | （无整改任务） | — | — | — | — |"


WIKI_LINKS = {
    "配方文档引用": "../../wiki/products/{p}.md",
    "工艺文档引用": "../../wiki/products/{p}.md",
    "GMP文档引用": "../../wiki/gmp/GMP质量约束与成本关联.md",
    "行业基准引用": "../../wiki/market/药材行情与行业基准.md",
}


def render_topic_month_table(fs: RF.FactSet, element: str) -> str:
    """
    专题报告 2.2「分月明细」：该要素逐月的值、环比、占单位成本比。

    ⚠️ 占用**该月自己的**单位成本做分母，不是用期间均值——
       否则占比会随时间漂移，看着像"要素占比在变"，其实是分母选错了。
    """
    col = {"直接材料": "直接材料(元/盒)", "直接人工": "直接人工(元/盒)",
           "制造费用": "制造费用(元/盒)"}.get(element)
    rows = getattr(fs, "_topic_rows", None)
    if rows is None:
        rows = _topic_rows(fs.product, fs.month)
    ser = []
    for r in rows:
        if (r.get("产品名称") or "").strip() != fs.product:
            continue
        v = RF._num(r.get(col))
        u = RF._num(r.get("单位成本(元/盒)"))
        ser.append((r["月份"], v, u))
    ser.sort(key=lambda x: x[0])

    out = []
    for i, (m, v, u) in enumerate(ser):
        if i == 0 or ser[i - 1][0] != RF._shift_month(m, -1):
            chg = "—"
        else:
            p = ser[i - 1][1]
            chg = f"{(v - p) / p * 100:+.2f}%" if p and v is not None else "—"
        share = f"{v / u * 100:.2f}%" if u and v is not None else "—"
        out.append(f"| {m} | {RF._fmt(v)} | {chg} | {share} |")
    return "\n".join(out) or "| — | — | — | — |"


def render_topic_metric_table(fs: RF.FactSet, element: str) -> str:
    """专题报告 2.1「专题指标一览」。"""
    g = lambda k: (fs.facts[k].display if fs.facts.get(k) else "—")
    last = fs.month
    return "\n".join([
        f"| {element}期间均值 | {g('专题期间均值')} 元/盒 | — | — | 算术均值 |",
        f"| {element}期间加权值 | {g('专题期间加权')} 元/盒 | — | — | 按产量加权 |",
        f"| 期间峰值 | {g('专题峰值')} 元/盒 | — | — | 见 2.2 分月明细 |",
        f"| 期间谷值 | {g('专题谷值')} 元/盒 | — | — | 见 2.2 分月明细 |",
        f"| 波动幅度 | {g('专题波动幅度')} 元/盒 | — | — | 峰值 − 谷值 |",
        f"| 期末月单位成本 | {g('本月单位成本')} 元/盒 | — | — | {last} |",
    ])


def _topic_conclusion(fs: RF.FactSet, meta: Dict[str, Any]) -> str:
    """
    专题结论——**纯脚本拼串**，不含任何 LLM 成分。

    ⚠️ 为什么结论不给 LLM：专题报告的失败模式是"结论跟着文字走"——
       模型先把叙述写漂亮，结论再顺着叙述圆回来，最后结论与数据脱节
       而读起来毫无破绽。所以结论先由数据钉死，后面的分析只能解释它。
    """
    t = meta.get("topic_name") or "成本要素"
    g = lambda k: (fs.facts[k].display if fs.facts.get(k) else "—")
    return (f"{t}在分析期内均值 {g('专题期间均值')} 元/盒、"
            f"按产量加权 {g('专题期间加权')} 元/盒；"
            f"峰值 {g('专题峰值')}、谷值 {g('专题谷值')}，"
            f"波动幅度 {g('专题波动幅度')} 元/盒。"
            f"（数值全部由脚本从原始成本表算出，未经模型改写。）")


def _quarter_of(month: str) -> str:
    """`'2026-05'` → `'2026-Q2'`。只给月份时推断所属季度。"""
    y, m = int(month[:4]), int(month[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"



TOPIC_ELEMENTS = ["直接材料", "直接人工", "制造费用"]


def _topic_rows(product: str, month: str) -> List[Dict[str, str]]:
    """One historical snapshot drives topic statistics and rendered rows."""
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        raise ValueError("专题截止月份不是合法 YYYY-MM")
    rows = [dict(row, __csv_record=record) for record, row in enumerate(
                RF._read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv"), 2)
            if (row.get("产品名称") or "").strip() == product
            and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", (row.get("月份") or "").strip())
            and (row.get("月份") or "").strip() <= month]
    periods = [(row.get("月份") or "").strip() for row in rows]
    if len(periods) != len(set(periods)):
        raise ValueError("专题数据存在同产品同月重复观测")
    return sorted(rows, key=lambda row: row["月份"])


def _build_topic_facts(product: str, month: str,
                       element: str) -> Tuple[RF.FactSet, Dict[str, Any]]:
    """
    专题分析：围绕**单一成本要素**跨月展开。

    做法：先取该要素的逐月序列，再算它的期间统计与环比。
    ⚠️ 专题**不重算**别的要素——那份数据在月度/季度报告里已经有了，
       重复算只会多一处可能漂移的口径。
    """
    if element not in TOPIC_ELEMENTS:
        element = TOPIC_ELEMENTS[0]
    fs = RF.build(product, month)
    col = {"直接材料": "直接材料(元/盒)", "直接人工": "直接人工(元/盒)",
           "制造费用": "制造费用(元/盒)"}[element]

    rows = _topic_rows(product, month)
    fs._topic_rows = rows
    fs.topic_rows, fs.topic_element = rows, element
    fs.period_kind, fs.period_label = "topic", month
    fs.input_periods = tuple(row["月份"] for row in rows)
    ser = [(r["月份"], as_decimal(r.get(col)), as_decimal(r.get("产量(盒)")))
           for r in rows
           if (r.get("产品名称") or "").strip() == product]
    ser.sort(key=lambda x: x[0])

    vals = [v for _, v, _ in ser]
    qty = [q for _, _, q in ser]
    complete_values = bool(vals) and all(value is not None for value in vals)
    mean = sum(vals) / len(vals) if complete_values else None
    total_q = sum(qty) if qty and all(q is not None and q >= 0 for q in qty) else None
    wavg = (sum(v * q for v, q in zip(vals, qty)) / total_q) if complete_values and total_q else None
    peak = max(ser, key=lambda x: x[1]) if complete_values else None
    low = min(ser, key=lambda x: x[1]) if complete_values else None

    from .policy import DEFAULT_DATASET_ID
    source_file = "csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv"
    topic_inputs = [{"dataset_id": DEFAULT_DATASET_ID, "source_uri": "raw/" + source_file,
                     "field": field, "selector": urlencode({"row": row["__csv_record"],
                         "产品名称": product, "月份": row["月份"]}), "kind": "raw", "formula": ""}
                    for row in rows for field in (col, "产量(盒)")]

    def add(name, value, unit="", formula=""):
        derived = {"dataset_id": DEFAULT_DATASET_ID,
                   "source_uri": f"derived:pharma-topic:{product}@{month}:{element}",
                   "field": name, "selector": urlencode({"产品名称": product, "截至月": month}),
                   "kind": "derived", "formula": formula}
        fs.facts[name] = RF.Fact(value=float(value) if value is not None else None,
                                 display=RF._fmt(value), unit=unit, formula=formula,
                                 source_file=source_file, source_kind="derived",
                                 period=(f"{ser[0][0]}..{ser[-1][0]}" if ser else month),
                                 input_sources=[*topic_inputs, derived])

    add("专题期间均值", mean, "元/盒", f"{len(vals)} 个月算术均值")
    add("专题期间加权", wavg, "元/盒", "Σ(单位值×月产量) ÷ 总产量")
    add("专题峰值", peak[1] if peak else None, "元/盒",
        f"{peak[0]} 取得" if peak else "")
    add("专题谷值", low[1] if low else None, "元/盒",
        f"{low[0]} 取得" if low else "")
    add("专题波动幅度", peak[1] - low[1] if peak and low else None, "元/盒", "峰值 − 谷值")

    if vals and total_q is None:
        fs.missing.append("专题产量资料不完整，无法计算期间加权值")
    if vals and not complete_values:
        fs.missing.append("专题所选要素存在缺失月份值，期间统计未完成；缺值行保留在分月明细")
    meta = {"theme": "topic", "topic_name": element, "topic_months": len(vals),
            "cutoff": month, "months": [period for period, _, _ in ser]}
    return fs, meta


def deterministic_map(fs: RF.FactSet, today: str,
                      theme: str = "monthly",
                      theme_meta: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """
    脚本能算的全在这里。返回 占位符名 → 字符串。

    ⚠️ `theme` 只影响**措辞类**占位符（标题/报告类型/期间名）。
       数字占位符三种主题完全共用——因为季度/专题的 FactSet 键名
       与月度一致（`本月X` 在季度里语义即「本季度X」）。
    """
    tm = theme_meta or {}
    label_zh = ""        # 季度是「2026 年二季度」这种中文期间名；其余主题留空
    code = PRODUCT_CODE.get(fs.product, "XX")
    ym = fs.month.replace("-", "")
    if theme == "quarterly":
        period_label = tm.get("label") or fs.month
        suffix = (tm.get("quarter") or "").replace("-Q", "Q").replace("Q", "-Q")
        m_title = f"中药一厂 {period_label} 季度产品成本多维深度分析报告 — {fs.product}"
        rtype = "季度产品成本多维深度分析报告"
        rno = f"RPT-{suffix}-{code}-001" if suffix else f"RPT-{ym}-{code}-001"
        _ms = tm.get("months") or [fs.month]
        span = f"{_ms[0]} ~ {_ms[-1]}"          # 只列首尾，不罗列全部月份
        label_zh = (tm.get("label") or "")
    elif theme == "topic":
        topic = tm.get("topic_name") or "成本专题"
        period_label = f"{fs.month}　{topic}专题"
        m_title = f"中药一厂 {fs.month} {topic}专题分析报告 — {fs.product}"
        rtype = f"{topic}专题分析报告"
        rno = f"RPT-{ym}-{code}-T"
        selected_months = tm.get("months") or [fs.month]
        span = f"{selected_months[0]} ~ {fs.month}"
    else:
        period_label = fs.month
        m_title = f"中药一厂 {fs.month} 月度成本分析报告 — {fs.product}"
        rtype = "月度成本分析报告"
        rno = f"RPT-{ym}-{code}-001"
        span = fs.month

    m: Dict[str, str] = {
        "报告标题": m_title,
        "报告编号": rno,
        "分析月份": period_label if theme != "quarterly" else (tm.get("quarter") or fs.month),
        "分析期间": label_zh or period_label,
        "期间起止": span,
        "编制日期": today,
        "报告类型": rtype,
        "专题名称": tm.get("topic_name", "—"),
        "专题结论": "",          # 由 build 里脚本拼串后填入（不在 LLM 手里）
        "选题理由": tm.get("topic_reason", "按所选成本要素开展截至期的跨月复核。"),
        "产品名称": fs.product,
        "产品规格": PRODUCT_SPEC.get(fs.product, "—"),
    }
    # 2.1 三行（含产量/单位成本/总成本）——模板名 → fact 名
    pairs = [
        ("本月产量", "产量(盒)"), ("上月产量", "上月产量(盒)"), ("产量环比", "产量(盒)环比"),
        ("去年同月产量", "去年同月产量"), ("产量同比", "产量同比"), ("预算产量", "预算产量"),
        ("产量预算偏差", "产量预算偏差"),
        ("本月单位成本", "本月单位成本"), ("上月单位成本", "上月单位成本"),
        ("单位成本环比", "单位成本环比"), ("去年单位成本", "去年单位成本"),
        ("单位成本同比", "单位成本同比"), ("预算单位成本", "预算单位成本"),
        ("单位成本预算偏差", "单位成本预算偏差"),
        ("本月总成本", "本月总成本"), ("上月总成本", "上月总成本"), ("总成本环比", "总成本环比"),
        ("去年总成本", "去年总成本"), ("总成本同比", "总成本同比"),
        ("预算总成本", "预算总成本"), ("总成本预算偏差", "总成本预算偏差"),
        ("本月材料成本", "本月材料成本"), ("上月材料成本", "上月材料成本"),
        ("材料成本环比", "材料成本环比"), ("预算材料成本", "预算材料成本"),
        ("材料预算偏差", "材料预算偏差"),
        ("本月人工成本", "本月人工成本"), ("上月人工成本", "上月人工成本"),
        ("人工成本环比", "人工成本环比"), ("预算人工成本", "预算人工成本"),
        ("人工预算偏差", "人工预算偏差"),
        ("本月制造费用", "本月制造成本"), ("上月制造费用", "上月制造成本"),
        ("制造费用环比", "制造费用环比"), ("预算制造费用", "预算制造成本"),
        ("制造费用预算偏差", "制造费用预算偏差"),
        # 2.2
        ("材料金额", "材料金额"), ("材料占比", "材料占比"), ("材料环比", "材料成本环比"),
        ("材料贡献度", "材料贡献度"),
        ("人工金额", "人工金额"), ("人工占比", "人工占比"), ("人工环比", "人工成本环比"),
        ("人工贡献度", "人工贡献度"),
        ("制造费用金额", "制造费用金额"), ("制造费用占比", "制造费用占比"),
        ("制造费用贡献度", "制造费用贡献度"),
        ("单位成本", "本月单位成本"), ("总环比", "单位成本环比"),
        # 3.2
        # ⚠️ 3.2 的「单位人工成本 环比」**不能**再插一条 ("人工环比", …)——
        #    占位符 `{{人工环比}}` 在 2.2（不带 %）和 3.2（带 %）各出现一次，
        #    但两处要的是**同一个事实**（人工成本环比），本来就不该重名映射两次。
        #    曾经这里写过 ("人工环比", "单位成本环比")，字典后写覆盖先写，

        #    这个错数进了 3 份已交付报告，且无人察觉——见下方 _check_unique 守卫。
        ("人工单位成本", "人工单位成本"), ("上月人工单位成本", "人工单位成本上月"),
        ("本月工时", "本月工时"), ("上月工时", "上月工时"), ("工时环比", "工时环比"),
        ("本月时薪", "本月时薪"), ("上月时薪", "上月时薪"), ("时薪环比", "时薪环比"),
        ("本月效率", "本月效率"), ("上月效率", "上月效率"), ("效率环比", "效率环比"),
        # 3.3
        ("本月折旧", "本月折旧费"), ("上月折旧", "上月折旧费"), ("折旧环比", "折旧费环比"),
        ("本月动力", "本月动力费(水电气)"), ("上月动力", "上月动力费(水电气)"),
        ("动力环比", "动力费(水电气)环比"),
        ("本月间接人工", "本月间接人工"), ("上月间接人工", "上月间接人工"),
        ("间接人工环比", "间接人工环比"),
        ("本月检验", "本月检验费"), ("上月检验", "上月检验费"), ("检验环比", "检验费环比"),
        ("本月其他", "本月其他制造费用"), ("上月其他", "上月其他制造费用"),
        ("其他环比", "其他制造费用环比"),
    ]
    # ⚠️ 值**保留 `%`**，是否去重交给 `fill()` 判断。
    #    原因：同一个占位符在不同节的模板写法不同——
    #      2.2 节 `{{人工环比}}`      （模板不带 %）
    #      3.2 节 `{{人工环比}}%`     （模板自带 %）

    _check_unique(pairs)
    for tpl_name, fact_name in pairs:
        v = _fact_display(fs, fact_name)
        if v is not None:
            m[tpl_name] = v


    m["波动告警结论"] = threshold_verdict(evaluate_threshold(fs))
    if theme == "quarterly":
        m["波动告警结论"] = m["波动告警结论"].replace("本月", "本季度")

    # 制造费用合计 = 五类之和（客户端算，不依赖 LLM）
    def complete_total(prefix):
        values = [as_decimal(fs.facts[f"{prefix}{name}"].value)
                  if fs.facts.get(f"{prefix}{name}") else None
                  for name in RF.MFG_TEMPLATE_NAMES.values()]
        return sum(values, Decimal(0)) if all(value is not None for value in values) else None
    tot, tot_p = complete_total("本月"), complete_total("上月")
    m["制造费用合计"] = format_number(tot)
    m["上月制造费用合计"] = format_number(tot_p)
    change = (tot - tot_p) / tot_p * 100 if tot is not None and tot_p not in (None, 0) else None
    m["制造费用合计环比"] = format_number(change, signed=True)

    # 表格填充位
    m["原材料成本明细表格"] = render_materials_table(fs)
    m["近6个月成本趋势表格"] = render_trend_table(fs)
    m["原材料价格跟踪表格"] = render_market_table(fs)
    m["对标差异表格"] = render_peer_table(fs)
    # 知识库引用（本项目 "RAG" 要求的落点）
    for k, tpl in WIKI_LINKS.items():
        m[k] = tpl.format(p=fs.product)
    return m


# ---------------------------------------------------------------------------
# 二、模板填充
# ---------------------------------------------------------------------------

_PH = re.compile(r"\{\{([^{}]+)\}\}")


def fill(template: str, values: Dict[str, str]) -> tuple[str, List[str]]:
    """Legacy API for the shared Unicode placeholder renderer."""
    return CR.render_template(template, values)


# ---------------------------------------------------------------------------
# 三、LLM 叙事生成
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM = """你是模拟药企成本报告撰写员。数字由确定性程序计算，你只写解释。
只使用给定事实与来源，不重新计算或补造数字。账面成本单位为元/盒；市场行情按原单位元/kg或元/万粒引用，不等于采购价。
将数值贡献、背景记录、模拟场景设定、待核查假设分开。无采购/用量/归集证据时不得确定根因。
设备故障须匹配产品和发生期间；没有记录不能证明全年无故障，历史关联不等于本期原因。
引用Disputed资料须明确口径争议，不以理论配方替代账面成本，不用未来月份解释过去。
不得建议降低检验、擅改验证周期或未经批准返工；质量相关建议仅限QA评估与补证。
输出简洁中文和所要求的JSON，不输出思考过程。建议预期效果不得编造节约金额或改善比例。
"""


def _llm_client():
    from dotenv import load_dotenv
    for p in [WIKI_ROOT, *WIKI_ROOT.parents]:
        env = p / ".env"
        if env.exists():
            load_dotenv(dotenv_path=env)
            break
    from langchain_openai import ChatOpenAI
    import httpx
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise ConfigError("未找到 DEEPSEEK_API_KEY",
                          hint="在工作区根目录建 .env，写入 DEEPSEEK_API_KEY=...")
    model = os.getenv("AGENT_MODEL", "deepseek-flash")
    if model != "deepseek-flash":
        raise ConfigError("本工作区仅允许 deepseek-flash")
    return ChatOpenAI(
        model=model,
        api_key=key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        temperature=0.2,
        max_retries=0, timeout=90,
        http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
        http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
    )


def _facts_brief(fs: RF.FactSet) -> str:
    """给 LLM 看的「算术结论」——不含原始 CSV，只有算好的值 + 坐标。"""
    lines = [f"产品 {fs.product}｜分析月 {fs.month}｜对照月 {fs.prev_month}｜去年同月 {fs.year_ago_month}", ""]
    lines.append("【核心指标】")
    for k in ("本月单位成本", "上月单位成本", "单位成本环比", "去年单位成本", "单位成本同比",
              "单位成本预算偏差", "本月材料成本", "上月材料成本", "材料成本环比",
              "本月人工成本", "人工成本环比", "本月制造成本", "制造费用环比"):
        f = fs.facts.get(k)
        if f:
            lines.append(f"  {k} = {f.display}{f.unit or ''}"
                         + (f"   （{f.formula}）" if f.formula else "") + f" 来源 {f.coord}")
    lines.append("")
    lines.append("【成本结构】")
    for k in ("材料占比", "人工占比", "制造费用占比", "材料贡献度", "人工贡献度", "制造费用贡献度"):
        f = fs.facts.get(k)
        if f:
            lines.append(f"  {k} = {f.display}")
    lines.append("")
    lines.append(f"【截至分析月的可见单位成本：{len(fs.monthly)}个月】")
    lines.append("  " + "  ".join(f"{r['月份']}={r['单位成本']}({r['环比']})" for r in fs.monthly))
    lines.append("")
    if fs.materials:
        lines.append("【原材料环比】")
        lines.append("  " + "；".join(f"{m['原材料名称']} {m['上月']}→{m['本月']} ({m['环比']})"
                                       for m in fs.materials))
        lines.append("")
    if fs.peer:
        lines.append("【一厂 vs 二厂】" + DIFF_NOTE)
        for p in fs.peer:
            lines.append(f"  {p['维度']}: 一厂 {p['中药一厂']} / 二厂 {p['中药二厂']} "
                         f"差异 {p['差异']} ({p['差异率']}) {p['方向']}")
        lines.append("")
    lines.append("【资料可用性】本数据包已提供两厂同产品同月产量，以及一厂工时、人员与制造费用明细。缺少二厂工时、人员与制造费用分项、企业实际采购合同和物理用量，不能泛称缺少两厂全部资料。")
    lines.append("【判断边界】未触发环比告警不等于整体成本已受控，仍须核对预算；材料净差额接近不能排除采购价与用量效应抵消。")
    lines.append(context_for(fs.product, fs.month, workspace_root=RF.current_workspace()))
    lines.append("")
    if fs.conflicts:
        lines.append("【口径争议（引用须注明）】")
        for c in fs.conflicts:
            lines.append(f"  · {c}")
        lines.append("")
    if fs.missing:
        lines.append("【数据缺失（不得编造）】")
        for m in fs.missing:
            lines.append(f"  · {m}")
    return "\n".join(lines)


def generate_analysis(fs: RF.FactSet, llm: Any, verbose: bool = True,
                      threshold: Optional[Dict[str, Any]] = None,
                      extra_fields: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """One bounded Flash request fills all narrative fields; no silent partial report."""
    from datetime import timedelta
    model = getattr(llm, "model_name", "deepseek-flash")
    if model != "deepseek-flash":
        raise ConfigError("本工作区仅允许 deepseek-flash")
    th = threshold if threshold is not None else evaluate_threshold(fs)
    fields = dict(LLM_SCALAR_FIELDS)
    fields.update(extra_fields or {})
    fields.update({name: "一句话，说明数值变化；无原因证据写需核查。" for name in LLM_MFG_NOTES})
    if th["breaches"]:
        fields["重点分析段落"] = "按已触发要素写120至200字，区分贡献与待核实原因。"
    due = (date.today() + timedelta(days=14)).isoformat()
    facts = fact_records(fs)
    evidence = {item["id"]: item for item in evidence_records(
        fs.product, fs.month, workspace_root=RF.current_workspace())}
    fact_catalog = [{key: value for key, value in item.items()
                     if key in {"id", "product", "period", "metric", "display", "unit", "coord"}}
                    for item in facts.values()]
    prompt = ("【确定性事实与本地证据】\n" + _facts_brief(fs)
              + "\n制造费用明细：" + json.dumps(fs.mfg, ensure_ascii=False)
              + "\n阈值判定：" + json.dumps(th, ensure_ascii=False)
              + "\n可用fact_id（数字、对象、方向和时期由程序按ID原样回填）："
              + json.dumps(fact_catalog, ensure_ascii=False)
              + "\n可用evidence_id：" + json.dumps(list(evidence.values()), ensure_ascii=False)
              + "\n请一次返回JSON对象。texts包含以下每个字段，各字段必须是对象："
                '{"fact_ids":[],"evidence_ids":[],"hypotheses":[],"checks":[]}。'
              + "每字段优先选择一个最相关fact_id，背景引用最多一个evidence_id。"
              + "hypotheses和checks只写简短定性假设或核查动作，各最多一条，不得写任何数字、"
                "月份、环比/同比结论或确定性因果；所有数字句由程序生成。"
              + "不得自己输出对象、方向或数值来替代fact_id。无原因证据时写待核查假设。"
              + "字段用途："
              + json.dumps(fields, ensure_ascii=False)
              + "\nadvice为3条核查建议数组，每项必须含建议事项、责任部门、优先级、预期效果、建议完成时间。"
              + "责任部门为生产部/采购部/财务部/质量部，优先级为高/中/低。"
              + "建议完成时间统一为" + due + "。建议仅为待人工审核方案，不执行派单。")
    response = llm.invoke([("system", ANALYSIS_SYSTEM), ("user", prompt)],
                         max_tokens=4096, response_format={"type": "json_object"},
                         extra_body={"thinking": {"type": "disabled"}})
    raw = (response.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    payload = json.loads(raw)
    texts = payload.get("texts", {})
    if not isinstance(texts, dict) or set(texts) != set(fields):
        raise ValueError("Flash返回的报告字段不完整，未发布报告")
    out = {key: render_grounded_field(texts[key], facts, evidence, fs.product, fs.month)
           for key in fields}
    advice = payload.get("advice")
    required = ("建议事项", "责任部门", "优先级", "预期效果", "建议完成时间")
    if not isinstance(advice, list) or len(advice) != 3:
        raise ValueError("Flash须返回三条可审核建议，未发布报告")
    for item in advice:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip() for k in required):
            raise ValueError("建议字段不完整，未发布报告")
        if item["责任部门"] not in ("生产部", "采购部", "财务部", "质量部") or item["优先级"] not in ("高", "中", "低"):
            raise ValueError("建议部门或优先级无效")
        for key in ("建议事项", "预期效果"):
            item[key] = validate_qualitative(item[key], fs.product, fs.month)
        item["建议完成时间"] = due
    out.setdefault("重点分析段落", "")
    total = sum(len(text) for item in texts.values()
                for key in ("hypotheses", "checks") for text in item[key])
    total += sum(len(item[key]) for item in advice for key in ("建议事项", "预期效果"))
    if total > MAX_TOTAL_LLM_CHARS:
        raise ValueError("报告叙述超出字数上限，未发布报告")
    rendered_chars = sum(len(value) for value in out.values())
    if rendered_chars > 12000:
        raise ValueError("报告引用与叙述超出总长度上限，未发布报告")
    out["_advice"] = json.dumps(advice, ensure_ascii=False)
    out["_total_chars"] = total
    out["_rendered_chars"] = rendered_chars
    out["_usage"] = getattr(response, "usage_metadata", None) or {}
    out["_model"] = model
    out["_calls"] = 1
    out["_grounding"] = {"mode": "fact_id_deterministic_render", "fields": texts,
                         "fact_ids": sorted(facts), "evidence_ids": sorted(evidence),
                         "causal_verification": "not_established; hypotheses_require_review"}
    if verbose:
        print(f"Flash结构化叙述完成：{total}字，1次调用")
    return out


# ---------------------------------------------------------------------------
# 四、编排
# ---------------------------------------------------------------------------

def _pharma_scope_guard(context: CR.ReportContext) -> None:
    """Keep entity/time semantics in the plugin, not the generic workflow."""
    fs = context.facts
    if getattr(fs, "product", context.request.entity_id) != context.request.entity_id:
        raise ValueError("报告产品与事实范围不一致")
    if context.request.theme == "monthly" and fs.month != context.request.period:
        raise ValueError("报告月份与事实范围不一致")
    for row in getattr(fs, "monthly", ()):
        if str(row.get("月份", "")) > fs.month:
            raise ValueError("报告事实包含分析月之后的数据")


def report_profile(theme: str = "monthly", template_docx: Optional[str] = None) -> CR.ReportProfile:
    """The complete default industry profile; core contains no pharma labels."""
    templates = {name: CR.ReportTemplate(path=path) for name, path in TEMPLATES.items()}
    if template_docx is not None:
        templates[theme] = CR.ReportTemplate(path=Path(template_docx), kind="docx")
    return CR.ReportProfile(
        profile_id="pharma_cost_v1", version="1",
        components=tuple(label for label, _ in ELEMENT_FACTS),
        units={"unit_cost": "元/盒", "quantity": "盒"},
        fields={**LLM_SCALAR_FIELDS, **(LLM_TOPIC_FIELDS if theme == "topic" else {})},
        templates=templates, semantic_guards=(_pharma_scope_guard,),
        model_budget={"max_calls": 1, "max_output_tokens": 4096},
        missing_data_message="核算数据缺失，未调用模型：")


def build(product: str, month: str, use_llm: bool = True,
          verbose: bool = True,
          template_docx: Optional[str] = None,
          docx_out_path: Optional[str] = None,
          theme: str = "monthly", period: str = "",
          workspace_root: Optional[Path] = None) -> Dict[str, Any]:
    if workspace_root is not None:
        with RF.data_workspace(workspace_root):
            return _build(product, month, use_llm, verbose, template_docx,
                          docx_out_path, theme, period)
    return _build(product, month, use_llm, verbose, template_docx, docx_out_path, theme, period)


def _build(product: str, month: str, use_llm: bool = True,
           verbose: bool = True, template_docx: Optional[str] = None,
           docx_out_path: Optional[str] = None,
           theme: str = "monthly", period: str = "") -> Dict[str, Any]:
    """Compatibility entrypoint backed by the shared ReportWorkflow.

    Industry-specific preparation and narrative remain here. The workflow owns
    preflight ordering, missing-data guards, optional execution and rendering.
    """
    if theme == "quarterly":
        import period_agg as PA
        cutoff = PA.quarter_months(period or _quarter_of(month))[-1]
        if month < cutoff:
            raise ValueError(f"截至月份 {month} 尚未覆盖完整季度（期末 {cutoff}），不能使用未来月份")
    profile = report_profile(theme, template_docx)
    request = CR.ReportRequest(
        entity_id=product, period=month, use_llm=use_llm, theme=theme,
        as_of=month, dataset_id="pharma-default", workspace_root=RF.current_workspace(),
        today=date.today().isoformat())
    theme_meta: Dict[str, Any] = {"theme": theme}
    state: Dict[str, Any] = {}

    def prepare_facts(req):
        if theme == "quarterly":
            import period_agg as PA
            fs, metadata = PA.build_quarter(product, period or _quarter_of(month))
            theme_meta.update(metadata)
            return fs
        if theme == "topic":
            fs, metadata = _build_topic_facts(product, month, period or "直接材料")
            theme_meta.update(metadata)
            return fs
        return RF.build(product, month)

    def prepare_values(fs, req):
        vals = deterministic_map(fs, req.today, theme, theme_meta)
        vals["重点分析段落"] = ""
        if theme == "topic":
            vals["专题结论"] = _topic_conclusion(fs, theme_meta)
            vals["专题结论复核"] = vals["专题结论"]
            element = theme_meta.get("topic_name") or "直接材料"
            vals["专题指标表格"] = render_topic_metric_table(fs, element)
            vals["专题分月表格"] = render_topic_month_table(fs, element)
            vals["专题对标表格"] = render_peer_table(fs)
            vals["量价拆解表格"] = (
                "| 因素 | 贡献(元/盒) | 占总变动比 | 判定 |\n"
                "|---|---:|---:|---|\n"
                "| 价格效应 | — | — | raw 无采购单价列，**无法分离** |\n"
                "| 用量效应 | — | — | raw 无单耗列，**无法分离** |\n"
                "| 合计 | — | — | 见 2.2 分月明细的实际变动 |")
        th = evaluate_threshold(fs)
        state["threshold"] = th
        if verbose:
            count = len(th["breaches"])
            print(f"  波动阈值判定（脚本，±{th['threshold']:.0f}%）："
                  + (f"{count} 项超阈值 → 将生成重点分析" if count else
                     ("资料不完整 → 待核查" if th["unknown"] else "无要素超阈值 → 不生成")))
        return vals

    def narrative(fs, req):
        extra = LLM_TOPIC_FIELDS if theme == "topic" else {}
        return generate_analysis(fs, _llm_client(), verbose=verbose,
                                 threshold=state["threshold"], extra_fields=extra)

    def finalize(context):
        calls = int(context.analysis.get("_calls", 0))
        grounded = context.analysis.get("_grounding", {}).get("mode") == "fact_id_deterministic_render"
        generated = bool(context.request.use_llm and calls > 0 and grounded)
        notice = ("本报告的数值、表格和图表由确定性程序根据本次冻结数据计算，"
                  "数据来源与版本见基本信息和来源附录。")
        if not calls:
            mode = "deterministic_only"
            notice += ("本次未调用语言模型，未生成归因、建议等分析叙述；"
                       "这些栏目中的“—”表示待补充分析，不代表零值或已排除异常。")
        elif generated:
            mode = "grounded_narrative"
            notice += (f"本次语言模型调用{calls}次，分析叙述中的数值、对象与期间由冻结事实按编号回填；"
                       "模型提出的原因假设与建议尚待核查，不构成已证实因果。")
        else:
            mode = "unverified_narrative"
            notice += (f"本次记录语言模型调用{calls}次，但未取得完整的事实绑定生成记录，"
                       "分析叙述不得视为已核验结论。")
        notice += "报告和建议须经人工审核，本次报告生成未执行任务派发。"
        context.values["编制说明"] = notice
        state["generation"] = {"llm_requested": bool(context.request.use_llm),
                               "narrative_generated": generated, "generation_mode": mode,
                               "generation_notice": notice}
        if not context.request.use_llm:
            return
        try:
            advice = json.loads(context.analysis.get("_advice", "[]"))
            if not isinstance(advice, list):
                advice = []
        except (json.JSONDecodeError, TypeError):
            advice = []
        context.values["改进建议表格"] = render_advice_table(advice)
        context.values["整改任务表格"] = render_task_table(advice, month, product)

    def render(template, context):
        if template.kind == "docx":
            import template_docx as TD
            output, missing = TD.fill_docx(
                template.path, context.values,
                Path(docx_out_path or (RF.current_workspace() / "reports" / f"{product}_{month}.docx")),
                blank_keys={"重点分析段落"})
            return CR.RenderedReport(f"（已按 Word 模板生成：{output.name}）", missing, output)
        report, missing = fill(template.text, context.values)
        return CR.RenderedReport(report, missing)

    def stats(context, rendered):
        fs, vals, analysis = context.facts, context.values, context.analysis
        return {
            "product": product, "month": month,
            "deterministic_fields": len([key for key in vals if not key.startswith("_")]),
            "missing": sorted(set(rendered.missing)),
            "llm_chars": analysis.get("_total_chars", 0), "facts": len(fs.facts),
            "conflicts": fs.conflicts, "data_missing": fs.missing,
            "llm_usage": analysis.get("_usage", {}), "llm_model": analysis.get("_model", ""),
            "llm_calls": analysis.get("_calls", 0),
            **state["generation"],
            "export_facts": fs.to_dict() if callable(getattr(fs, "to_dict", None)) else {},
            "grounding": analysis.get("_grounding", {}),
        }

    return CR.ReportWorkflow().run(
        request, profile, prepare_facts=prepare_facts, prepare_values=prepare_values,
        narrative=narrative, finalize_values=finalize, renderer=render, stats_builder=stats)


def main() -> int:
    ap = argparse.ArgumentParser(description="6 章月度成本分析报告生成器")
    ap.add_argument("--product", required=True)
    ap.add_argument("--month", default="2026-05")
    ap.add_argument("--out", help="输出路径（默认 reports/<product>_<month>.md）")
    ap.add_argument("--dry-run", action="store_true", help="只填确定性部分，不调 LLM")
    ap.add_argument("--json", action="store_true", help="额外输出统计 JSON")
    ap.add_argument("--template-docx", metavar="PATH",
                    help="改用 Word 模板（赛题 5.1.1）；不传则用默认 .md 模板")
    ap.add_argument("--theme", choices=["monthly", "quarterly", "topic"],
                    default="monthly", help="分析主题（赛题 5.1.3）")
    ap.add_argument("--period", default="",
                    help="季度用 2026-Q2；专题用要素名（直接材料/直接人工/制造费用）")
    a = ap.parse_args()

    if a.dry_run:
        print("=" * 74)
        print(f"确定性填充（--dry-run，不调用 LLM）  {a.product} {a.month}")
        print("=" * 74)
        r = build(a.product, a.month, use_llm=False, verbose=False,
                  template_docx=a.template_docx, docx_out_path=a.out,
                  theme=a.theme, period=a.period)
        print(f"  已填占位符 {r['stats']['deterministic_fields']} 个")
        print(f"  仍待 LLM 填 {len(r['stats']['missing'])} 个：")
        for m in r["stats"]["missing"]:
            print(f"    · {m}")
        return 0

    print("=" * 74)
    print(f"生成报告  {a.product}  {a.month}")
    print("=" * 74)
    r = build(a.product, a.month, use_llm=True,
              template_docx=a.template_docx, docx_out_path=a.out,
              theme=a.theme, period=a.period)
    if r.get("docx"):
        # Word 模板模式：产物就是那份 docx，不再写 md
        out = Path(r["docx"])
    else:
        out = Path(a.out) if a.out else (OUT_DIR / f"{a.product}_{a.month}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(r["report"], encoding="utf-8", newline="\n")

    print()
    try:
        shown = out.relative_to(WIKI_ROOT)
    except ValueError:
        shown = out            # 用户在仓库外指定的绝对路径
    print(f"✅ 已生成 {shown}")
    print(f"   确定性占位符 {r['stats']['deterministic_fields']} 个")
    print(f"   LLM 分析文本 {r['stats']['llm_chars']} 字")
    if r["stats"]["missing"]:
        print(f"   ⚠️ 未填占位符 {len(r['stats']['missing'])} 个: "
              f"{', '.join(r['stats']['missing'][:6])}")
    if r["stats"]["data_missing"]:
        print(f"   ⚠️ 数据缺失 {len(r['stats']['data_missing'])} 项（未编造）")
    if a.json:
        print(json.dumps(r["stats"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
