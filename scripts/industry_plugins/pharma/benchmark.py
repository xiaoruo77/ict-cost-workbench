# -*- coding: utf-8 -*-
"""Legacy compatibility implementation; original source materials are not included."""


































from __future__ import annotations
from model_policy import require_flash

import argparse
import csv
import json
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from console_io import ensure_utf8_stdout  # noqa: E402
from errors import ConfigError  # noqa: E402
import report_facts as RF  # noqa: E402
from cost_conventions import (as_decimal, compare_cost, format_number, quantize, DIFF_BASE,
                              DIFF_FORMULA, DIFF_NOTE)  # noqa: E402
from grounding import context_for  # noqa: E402
from cost_core import engine as core_engine  # noqa: E402
from .adapter import PharmaAdapter  # noqa: E402
from .policy import LEGACY_DIRECTIONS  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parents[2]
WIKI_ROOT = HERE.parent
RAW = WIKI_ROOT / "raw"

SELF = "中药一厂"
PEER = "中药二厂"


ELEMENTS = [
    ("直接材料", "直接材料(元/盒)"),
    ("直接人工", "直接人工(元/盒)"),
    ("制造费用", "制造费用(元/盒)"),
]


def _read(name: str) -> List[Dict[str, str]]:
    with open(RAW / name, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _num(v: Any) -> Optional[float]:
    number = as_decimal(v)
    return float(number) if number is not None else None


def _pick(rows, **kw):
    for r in rows:
        if all((r.get(k) or "").strip() == str(v) for k, v in kw.items()):
            return r
    return None


# ---------------------------------------------------------------------------
# 第一步：找差异
# ---------------------------------------------------------------------------

# 差异口径 —— **必须与本库 wiki 一致**，不能各算各的
#
# 本库 `wiki/benchmarking/一厂vs二厂对标分析.md` 用的是：
#     差异金额 = 二厂 − 一厂        （正数 = 二厂更贵）
#     差异率   = (二厂 − 一厂) ÷ **一厂**   ← 以本单位为基
#     方向     = "二厂高" / "一厂高"


#
# ⚠️ 踩过的坑：第一版引擎写成 `(一厂 − 二厂) ÷ 二厂`，得到 `-3.36%`——
#    符号与基准**都反了**。这类"基准取谁"的陷阱本库已登记过多次
#    （见 `SKILL.md` 关于涨幅基准必须注明的约定）。
#    所以这里把口径写成常量并在输出里标明。


def step1_find_diff(product: str, month: str) -> Dict[str, Any]:
    """差异总览表：产品 × 成本要素 × 一厂 / 二厂 / 差异金额 / 差异率 / 方向。"""
    a_rows = _read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv")
    b_rows = _read("csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv")
    adapter = PharmaAdapter(row_provider=_read, raw_root=RAW)
    own_observations = adapter.normalize_rows(
        "csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv", a_rows,
        organization=SELF, allow_inferred_identity=True)
    peer_observations = adapter.normalize_rows(
        "csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv", b_rows,
        organization=PEER, allow_inferred_identity=True)
    own_observation = next((o for o in own_observations if o.cost_object == product and o.period == month), None)
    peer_observation = next((o for o in peer_observations if o.cost_object == product and o.period == month), None)
    a = _pick(a_rows, 产品名称=product, 月份=month)
    b = _pick(b_rows, 产品名称=product, 月份=month)

    table: List[Dict[str, Any]] = []
    if a and b:
        for label, col in [("单位成本", "单位成本(元/盒)")] + ELEMENTS:
            va, vb = _num(a.get(col)), _num(b.get(col))
            if va is None or vb is None:
                continue
            comparison = core_engine.compare_values(a.get(col), b.get(col))
            source_key = ({"直接材料(元/盒)": "category.materials.unit_cost",
                           "直接人工(元/盒)": "category.labor.unit_cost",
                           "制造费用(元/盒)": "category.manufacturing_overhead.unit_cost"}.get(col, "unit_cost"))
            refs = ((own_observation.sources.get(source_key, ()) if own_observation else ())
                    + (peer_observation.sources.get(source_key, ()) if peer_observation else ()))
            table.append({
                "成本要素": label,
                "一厂": float(quantize(comparison["self_value"])) if comparison["self_value"] is not None else None,
                "二厂": float(quantize(comparison["peer_value"])) if comparison["peer_value"] is not None else None,
                "差异金额": float(quantize(comparison["difference"])) if comparison["difference"] is not None else None,
                "差异率": float(quantize(comparison["rate"])) if comparison["rate"] is not None else None,
                "方向": LEGACY_DIRECTIONS[comparison["direction"]],
                "source_kind": ("synthetic" if any(ref.kind == "synthetic" for ref in refs)
                                else "inferred" if any(ref.kind == "inferred" for ref in refs) else "raw"),
                "period": month,
                "input_sources": [asdict(ref) for ref in refs],
                # 溯源：差多少要说得出是从哪两行算的
                "坐标": (f"[中药一厂_成本汇总_2026年1-6月.csv:{col}:{product}&{month}] "
                         f"+ [中药二厂_成本汇总_2026年1-6月.csv:{col}:{product}&{month}]"),
            })
    return {
        "step": 1, "name": "找差异",
        "input": "本单位成本数据 + 对标工厂成本数据",
        "output": "差异总览表（产品×成本要素×差异金额×差异率）",
        "product": product, "month": month,
        "diff_convention": {"基准": DIFF_BASE, "公式": DIFF_FORMULA,
                           "说明": DIFF_NOTE},
        "rows": table,
        "self_total": _num(a.get("单位成本(元/盒)")) if a else None,
        "peer_total": _num(b.get("单位成本(元/盒)")) if b else None,
    }


# ---------------------------------------------------------------------------
# 第二步：拆结构
# ---------------------------------------------------------------------------

def step2_breakdown(product: str, month: str,
                    s1: Dict[str, Any]) -> Dict[str, Any]:
    """
    差异结构树：把总差异按要素逐层拆解，定位主要来源。

    树的形状：
        单位成本差异  X 元/盒
        ├── 直接材料   Δ, 占差异的 P%   ← 主因/次因
        │   └── （一厂材料构成下钻：金银花 46.9% …）
        ├── 直接人工   Δ, 占差异的 P%
        └── 制造费用   Δ, 占差异的 P%

    ⚠️ 材料下钻只到**一厂自己的**构成——二厂没有明细（见模块 docstring）。
       节点上带 `benchmark_available: false` 如实标注。
    """
    total = None
    for r in s1["rows"]:
        if r["成本要素"] == "单位成本":
            total = r["差异金额"]
            break
    if total is None:
        return {"step": 2, "name": "拆结构", "tree": None,
                "note": "缺少同产品同期成本，无法比较", "rows": []}
    if abs(total) < 1e-9:
        return {"step": 2, "name": "拆结构", "tree": None,
                "note": "两厂单位成本无差异，无需拆解", "rows": []}

    nodes: List[Dict[str, Any]] = []
    for label, _col in ELEMENTS:
        r = next((x for x in s1["rows"] if x["成本要素"] == label), None)
        if not r or r["差异金额"] is None:
            continue
        # ⚠️ 占比用**绝对值**算。差异金额带符号（正=二厂高），
        #    若直接相除，符号相反的两项会得出负占比，读起来像"反向贡献"，
        #    而这里要表达的只是"这一项在总差异里的权重"。
        nodes.append({
            "要素": label,
            "差异金额": r["差异金额"],
            "占差异比": float(quantize(abs(as_decimal(r["差异金额"])) / abs(as_decimal(total)) * 100)),
            "方向": r["方向"],
        })
    nodes.sort(key=lambda n: abs(n["差异金额"]), reverse=True)
    if nodes:
        nodes[0]["结论"] = "主因"
        for n in nodes[1:]:
            n["结论"] = "次因" if n["占差异比"] >= 10 else "影响小"

    # ---- 下钻层：一厂材料构成（二厂无明细，如实标注）----
    drill: List[Dict[str, Any]] = []
    try:
        fs = RF.build(product, month)
        for m in fs.materials:
            drill.append({
                "原材料名称": m["原材料名称"],
                "一厂单耗": m["本月"],
                "占比": m["占比"],
                "环比": m["环比"],
            })
    except Exception:  # noqa: BLE001
        pass

    return {
        "step": 2, "name": "拆结构",
        "input": "差异总览表",
        "output": "差异结构树（可下钻至原材料明细）",
        "total_diff": total,
        "tree": {
            "根": f"单位成本差异 {format_number(total, signed=True)} 元/盒（二厂 − 一厂；正数=二厂更高）",
            "要素层": nodes,
            "下钻层": {
                "说明": ("材料项下钻显示的是**一厂自身的**材料构成。"
                         "二厂未提供原材料明细（只有成本汇总），"
                         "故无法给出二厂逐项材料对比。"),
                "benchmark_available": False,   # ⚠️ 如实标注，不假装能对比
                "明细": drill,
            },
        },
        "rows": nodes,
    }


# ---------------------------------------------------------------------------
# 第三步：拆原因（LLM）
# ---------------------------------------------------------------------------

STEP3_SYSTEM = """你是中药一厂的财务分析员，正在撰写「对标分析」的归因段落。

铁律：
1. **只使用我给你的数字**，不得自己计算、不得引入外部数字。
2. **不得编造根因**。归因只能用给定的「已知归因」；没有就写"需进一步核查"。
3. 单位一律 `元/盒`，**不得换算**。
4. 设备故障只有在给定证据匹配产品、发生期间且说明成本影响时，才可作为已证实原因。
   历史关联不等于本期因果；未提供记录不等于全年无故障。
5. 若事实中标注了数据缺失/不可比，**必须如实说明**，不得绕开。
6. 输出结构化短段，不要标题、不要代码块。全程中文。
"""


def _llm():
    import os
    from dotenv import load_dotenv
    for p in [WIKI_ROOT, *WIKI_ROOT.parents]:
        e = p / ".env"
        if e.exists():
            load_dotenv(dotenv_path=e)
            break
    from langchain_openai import ChatOpenAI
    import httpx
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise ConfigError("未找到 DEEPSEEK_API_KEY",
                       hint="在工作区根目录建 .env，写入 DEEPSEEK_API_KEY=...")
    return ChatOpenAI(
        model=require_flash(os.getenv("AGENT_MODEL", "deepseek-flash")), api_key=key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        temperature=0.2,
        http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
        http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
    )


def step3_attribution(product: str, month: str, s1: Dict, s2: Dict,
                      use_llm: bool = True, verbose: bool = True) -> Dict[str, Any]:
    """第三步：LLM 生成归因文本 + 改进建议（可转 RPA 指令）。"""
    if not s2.get("tree"):
        return {"step": 3, "name": "拆原因", "text": s2.get("note", "缺少可比数据，无法归因。"),
                "advice": []}

    facts = [f"产品 {product}｜月份 {month}｜对标 {PEER}", ""]
    facts.append(DIFF_NOTE)
    facts.append("【差异总览】")
    for r in s1["rows"]:
        rate_text = (format_number(r["差异率"], signed=True) + "%"
                     if r["差异率"] is not None else "不可计算（一厂为0）")
        facts.append(f"  {r['成本要素']}: 一厂 {r['一厂']} / 二厂 {r['二厂']} / "
                     f"差异 {format_number(r['差异金额'], signed=True)} ({rate_text}) {r['方向']}")
    facts.append("")
    facts.append("【差异结构】总差异 "
                 f"{s2['total_diff']:+.2f} 元/盒，各要素占比：")
    for n in s2["tree"]["要素层"]:
        facts.append(f"  {n['要素']}: {n['差异金额']:+.2f}（占 {n['占差异比']:.1f}%）"
                     f" → {n['结论']}")
    if s2["tree"]["下钻层"]["明细"]:
        facts.append("")
        facts.append("【一厂材料构成（二厂无明细，不可对比）】")
        for m in s2["tree"]["下钻层"]["明细"][:6]:
            facts.append(f"  {m['原材料名称']}: {m['一厂单耗']} 元/盒 占 {m['占比']}")
    facts.append("")
    facts.append("【归因证据（按产品与分析期间筛选）】")
    facts.append(context_for(product, month))
    facts.append("")
    facts.append("【数据边界】")
    facts.append("  · 二厂只提供成本汇总，**没有原材料明细**，故材料下钻只能给一厂自己的构成")
    facts.append("  · 同期金额差异已由脚本计算；采购、用量或分摊差异的原因须另有双方明细证据。")
    brief = "\n".join(facts)

    out: Dict[str, Any] = {"step": 3, "name": "拆原因", "brief": brief}

    if not use_llm:
        out["text"] = "（未调用 LLM）"
        out["advice"] = []
        return out

    llm = _llm()
    prompt = (
        f"## 事实（脚本算好的，只许引用，不许重算）\n\n{brief}\n\n"
        f"## 任务\n写一段 200-280 字的**差异归因分析**：\n"
        f"1) 先点明总差异与主因要素\n"
        f"2) 再说明该要素差异的内部构成\n"
        f"3) 指出哪些结论有数据支撑、哪些**无法判定**（并说明缺什么数据）\n"
        f"直接输出正文。"
    )
    try:
        r = llm.invoke([("system", STEP3_SYSTEM), ("user", prompt)])
        out["text"] = (r.content or "").strip()
    except Exception as e:  # noqa: BLE001
        out["text"] = f"（归因生成失败：{type(e).__name__}）"

    ap = (
        f"## 事实\n\n{brief}\n\n"
        "## 任务\n给出 3 条**可执行**的改进建议。只输出 JSON 数组，每项字段：\n"
        '{"建议事项":"…","责任部门":"生产部|采购部|财务部|质量部",'
        '"优先级":"高|中|低","预期效果":"…","建议完成时间":"YYYY-MM-DD"}\n'
        "不得输出 JSON 以外的内容。建议须针对本产品本月实际差异。"
    )
    try:
        r = llm.invoke([("system", STEP3_SYSTEM), ("user", ap)])
        raw = (r.content or "").strip()
        import re as _re
        raw = _re.sub(r"^```(?:json)?|```$", "", raw, flags=_re.M).strip()
        adv = json.loads(raw)
        out["advice"] = adv if isinstance(adv, list) else []
    except Exception as e:  # noqa: BLE001
        out["advice"] = []
        if verbose:
            print(f"    ⚠️ 建议生成失败: {e}")

    return out


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def run(product: str, month: str, use_llm: bool = True,
        verbose: bool = True) -> Dict[str, Any]:
    s1 = step1_find_diff(product, month)
    s2 = step2_breakdown(product, month, s1)
    if verbose:
        print(f"  第一步 找差异：{len(s1['rows'])} 项")
        print(f"  第二步 拆结构："
              f"{'主因 ' + s2['tree']['要素层'][0]['要素'] if s2.get('tree') else '无差异'}")
    s3 = step3_attribution(product, month, s1, s2, use_llm, verbose)
    return {"product": product, "month": month,
            "steps": [s1, s2, s3],
            # ⚠️ 用 `.get()` 而不是 `s2["input"]`：`step2_breakdown` 有**早退分支**
            #    （两厂单位成本无差异时不建树），那个分支的返回字典里没有
            #    input/output。硬取会抛 KeyError，把"无差异"这个正常结果
            #    变成 500 —— 实测踩过。
            "steps_meta": [
                {"step": 1, "name": "找差异",
                 "input": s1.get("input", ""), "output": s1.get("output", "")},
                {"step": 2, "name": "拆结构",
                 "input": s2.get("input", "差异总览表"),
                 "output": s2.get("output", "差异结构树")},
                {"step": 3, "name": "拆原因",
                 "input": "差异结构树 + 知识库检索结果",
                 "output": "归因分析文本 + 改进建议"},
            ]}


def _print(r: Dict[str, Any]) -> None:
    print("=" * 76)
    print(f"对标分析三步法  {r['product']}  {r['month']}")
    print("=" * 76)
    s1, s2, s3 = r["steps"]
    print("\n【第一步 · 找差异】")
    print(f"  {'要素':<10}{'一厂':>8}{'二厂':>8}{'差异':>10}{'差异率':>10}  方向")
    for x in s1["rows"]:
        print(f"  {x['成本要素']:<10}{x['一厂']:>8.2f}{x['二厂']:>8.2f}"
              f"{format_number(x['差异金额'], signed=True):>10}"
              f"{(format_number(x['差异率'], signed=True) + '%' if x['差异率'] is not None else '—'):>10}  {x['方向']}")
    print("\n【第二步 · 拆结构】")
    if s2.get("tree"):
        print(f"  {s2['tree']['根']}")
        for n in s2["tree"]["要素层"]:
            print(f"   ├─ {n['要素']:<8}{n['差异金额']:>+8.2f}  "
                  f"占 {n['占差异比']:>6.1f}%  【{n['结论']}】")
        d = s2["tree"]["下钻层"]
        print(f"   └─ 材料下钻（{len(d['明细'])} 项，可对比={d['benchmark_available']}）")
        for m in d["明细"][:5]:
            print(f"        {m['原材料名称']:<14}{m['一厂单耗']:>7} 元/盒  占 {m['占比']}")
        print(f"      ⚠️ {d['说明'][:60]}…")
    else:
        print("  两厂无差异")
    print("\n【第三步 · 拆原因】")
    print("  " + (s3.get("text") or "").replace("\n", "\n  "))
    if s3.get("advice"):
        print(f"\n  改进建议 {len(s3['advice'])} 条：")
        for i, a in enumerate(s3["advice"], 1):
            print(f"    {i}. [{a.get('优先级','?')}] {a.get('建议事项','')[:56]}"
                  f"  → {a.get('责任部门','')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="对标分析三步法引擎")
    ap.add_argument("--product", help="产品名")
    ap.add_argument("--month", default="2026-05")
    ap.add_argument("--all", action="store_true", help="全部产品")
    ap.add_argument("--no-llm", action="store_true", help="跳过第三步（只跑确定性两步）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="写入 JSON 文件")
    a = ap.parse_args()

    products = RF.list_products() if a.all else ([a.product] if a.product else [])
    if not products:
        ap.print_help()
        return 1

    results = []
    for p in products:
        r = run(p, a.month, use_llm=not a.no_llm)
        results.append(r)
        if not a.json and not a.out:
            _print(r)
            print()

    payload = results[0] if len(results) == 1 else results
    if a.out:
        Path(a.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                               encoding="utf-8", newline="\n")
        print(f"已写出 {a.out}")
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
