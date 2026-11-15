# -*- coding: utf-8 -*-
"""
graph_build.py — **因果知识图谱（派生视图）**

定位
----
图谱是**派生视图**，与 `state/`、`.index/`、SQLite 镜像同级：

    raw/ + wiki/  ← 唯一事实源
          ↓ 脚本抽取（每条边带坐标）
        graph/    ← 派生、可再生、**绝不手改**

**与 wiki 冲突时以 wiki 为准。**

为什么不手写图谱
----------------
本项目的 `demo/graph_kg.py` 是手写的，里面有一条因果链：

    金银花 → 水煎醇沉 → TQ-01提取罐 → 5月热电偶漂移 → 金银花消耗超标

逐项核对 raw 后确认**整条链都是编的**：`TQ-01` 编号不存在（真实 `EQ-TQ-001`）、
维修记录里没有提取罐故障、`+3.88%` 也算不出来、连出处文件名都是虚构的。

⚠️ **图谱比普通文档更危险**：普通文档编一个数还容易被 grep 抓到；
图谱编一条边看起来像"结构化知识"，自带权威感，再套上 provenance
就成了"有出处的因果链"，反而更难质疑。

所以本模块从 raw/wiki **抽取**而非手写，并在抽取后做**结构校验**：
引用了不存在的设备编号/实体 → 直接报错。
**一个真正从 raw 派生的图谱，会自动抓出上面那个 TQ-01 错误。**

节点类型
--------
    product     产品       银黄口服液 …
    material    原材料     金银花 …
    process     工序       水提取、浓缩 …
    equipment   设备       EQ-TQ-001 …
    incident    设备故障    4 条维修记录
    anomaly     成本异动    官方异动清单
    peer        对标单位    中药二厂

边类型
------
    product  --处方含(占比)--> material
    product  --生产工序------> process
    process  --关键设备------> equipment
    equipment --发生故障-----> incident
    anomaly  --涉及产品------> product
    incident --导致停机------> anomaly      （仅当有证据时）

用法
----
    python scripts/graph_build.py                 # 构建并校验
    python scripts/graph_build.py --json          # 打印全图
    python scripts/graph_build.py --trace 银黄口服液   # 多跳因果穿透
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
_CODE_ROOT = HERE.parent
# ⚠️ 与 ingest_tools / state 同一约定：`LLM_WIKI_WS` 可指向个人工作区。
#    个人区的图谱只抽个人 wiki/raw——**不读共享基线**，否则个人图谱会
#    把公司的词条也算进去，产出一个"混合体"。
WIKI_ROOT = Path(os.getenv("LLM_WIKI_WS") or _CODE_ROOT)
WIKI = WIKI_ROOT / "wiki"
RAW = WIKI_ROOT / "raw"
GRAPH_DIR = WIKI_ROOT / "graph"

EQUIP_CODE = re.compile(r"`?(EQ-[A-Z]{2}-\d{3})`?")
PEER = "中药二厂"
SCHEMA_VERSION = "pharma-graph/2"
_MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
_MATERIAL_FILE = "raw/csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv"
_PEER_FILE = "raw/csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv"


def _workspace(workspace_root=None) -> Path:
    return Path(workspace_root or WIKI_ROOT).resolve()


def _source(workspace_root, relative: str) -> Path:
    from overlay import contained_path
    return contained_path(_workspace(workspace_root), relative)


def _input_paths(workspace_root=None) -> List[Path]:
    root = _workspace(workspace_root)
    for folder in (root / "raw", root / "wiki"):
        if not folder.is_dir():
            raise FileNotFoundError(f"图谱输入目录不存在: {folder}")
    fixed = [_MATERIAL_FILE, _PEER_FILE,
             "wiki/equipment/设备台账与维修历史.md", "wiki/costs/成本异动登记册.md"]
    products = [_source(root, p.relative_to(root).as_posix())
                for p in sorted((root / "wiki/products").glob("*.md"))
                if not p.name.startswith(".")]
    return sorted([_source(root, rel) for rel in fixed] + products,
                  key=lambda p: p.relative_to(root).as_posix())


def input_signature(workspace_root=None) -> str:
    """Hash exact graph inputs and relative names, including missing files."""
    root = _workspace(workspace_root)
    digest = hashlib.sha256()
    for path in _input_paths(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(b"file\0" + path.read_bytes() if path.is_file() else b"missing\0")
        digest.update(b"\0")
    return digest.hexdigest()


def _material_rows(workspace_root=None):
    with _source(workspace_root, _MATERIAL_FILE).open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def _as_of(workspace_root=None, as_of=None) -> str:
    if as_of is not None:
        if not isinstance(as_of, str) or not _MONTH.fullmatch(as_of):
            raise ValueError("图谱截至期必须为合法 YYYY-MM")
        return as_of
    months = {(row.get("月份") or "").strip() for row in _material_rows(workspace_root)}
    if not months or any(not _MONTH.fullmatch(month) for month in months):
        raise ValueError("材料输入缺少合法月份，无法确定图谱截至期")
    return max(months)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id: str
    type: str
    label: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    coord: str = ""          # 溯源坐标：这个节点是从哪抽出来的


@dataclass
class Edge:
    src: str
    dst: str
    relation: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    coord: str = ""          # 溯源坐标：这条边凭什么成立


class Graph:
    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        self.edges: List[Edge] = []
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def add_node(self, n: Node) -> None:
        if n.id in self.nodes:
            return
        self.nodes[n.id] = n

    def add_edge(self, e: Edge) -> None:
        self.edges.append(e)

    # ---- 出/入度查询 ----
    def out(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges if e.src == node_id]

    def inc(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges if e.dst == node_id]

    def by_type(self, t: str) -> List[Node]:
        return [n for n in self.nodes.values() if n.type == t]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_from": "wiki/ + raw/（派生视图，可再生）",
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [asdict(e) for e in self.edges],
            "stats": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "by_type": {t: len(self.by_type(t)) for t in
                            sorted({n.type for n in self.nodes.values()})},
                "by_relation": {r: sum(1 for e in self.edges if e.relation == r)
                                for r in sorted({e.relation for e in self.edges})},
            },
            "warnings": self.warnings,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# 抽取器
# ---------------------------------------------------------------------------

def _rows(md_path: Path) -> List[List[str]]:
    """把 markdown 里的所有表格行抽出来（`| a | b |` → ['a','b']）。"""
    out = []
    for line in md_path.read_text(encoding="utf-8", errors="replace").split("\n"):
        s = line.strip()
        if s.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", s):
            cells = [c.strip() for c in s.strip("|").split("|")]
            out.append(cells)
    return out


def extract_equipment(g: Graph, workspace_root=None) -> None:
    """
    从设备台账表抽设备节点。
    真实表头：| 设备编号 | 设备名称 | 规格型号 | 数量 | 制造商 | 投产年份 | 原值(万元) | 月折旧(元) |
    """
    p = _source(workspace_root, "wiki/equipment/设备台账与维修历史.md")
    if not p.exists():
        g.errors.append("找不到设备台账词条")
        return
    coord_p = "wiki/equipment/设备台账与维修历史.md"
    for r in _rows(p):
        if len(r) < 3 or not EQUIP_CODE.fullmatch(r[0].strip("`")):
            continue
        code = r[0].strip("`")
        g.add_node(Node(
            id=code, type="equipment", label=r[1],
            attrs={"规格型号": r[2], "数量": r[3] if len(r) > 3 else "",
                   "制造商": r[4] if len(r) > 4 else "",
                   "投产年份": r[5] if len(r) > 5 else "",
                   "月折旧(元)": r[7] if len(r) > 7 else ""},
            coord=f"[{coord_p}:设备台账:{code}]"))


def extract_materials(g: Graph, workspace_root=None, as_of=None) -> None:
    """
    从原材料消耗明细抽 产品→原料 边。
    占比取明确截至月；省略时取输入中的最新合法月份。
    """
    period = _as_of(workspace_root, as_of)
    rows = _material_rows(workspace_root)
    for r in rows:
        if (r.get("月份") or "").strip() != period:
            continue
        prod = (r.get("产品名称") or "").strip()
        mat = (r.get("原材料名称") or "").strip()
        if not prod or not mat:
            continue
        g.add_node(Node(id=prod, type="product", label=prod,
                        coord=f"[中药一厂_成本汇总_2026年1-6月.csv:产品名称:{prod}]"))
        g.add_node(Node(id=mat, type="material", label=mat,
                        coord=f"[中药一厂_原材料消耗明细_2026年1-6月.csv:原材料名称:{mat}]"))
        g.add_edge(Edge(
            src=prod, dst=mat, relation="处方含",
            attrs={"占总材料成本": (r.get("占总材料成本比例") or "").strip(),
                   "单位消耗成本(元/盒)": (r.get("单位消耗成本(元/盒)") or "").strip(),
                   "期间": period},
            coord=(f"[中药一厂_原材料消耗明细_2026年1-6月.csv:"
                   f"占总材料成本比例:{prod}&{mat}&{period}]")))


def extract_processes(g: Graph, workspace_root=None) -> None:
    """
    从各产品词条的**工艺表**抽 产品→工序 与 工序→设备。

    表头形如：| 工序 | 关键工艺参数(CPP) | 收率/消耗指标 | 关键设备 | 对成本影响 |
    关键设备列里写的是真实编号（如 `EQ-TQ-001`）——这正是图谱最该利用的现成结构。
    """
    root = _workspace(workspace_root)
    for candidate in sorted((root / "wiki/products").glob("*.md")):
        if candidate.name.startswith("."):
            continue
        f = _source(root, candidate.relative_to(root).as_posix())
        prod = f.stem
        coord_p = f"wiki/products/{prod}.md"
        for r in _rows(f):
            if len(r) < 5 or r[0] in ("工序", "月份"):
                continue
            name, cpp, yield_, equip_cell, impact = r[0], r[1], r[2], r[3], r[4]
            # 工序名要像工序（避免把数据表的行误当工序）
            if not name or len(name) > 14:
                continue

            # 判据：**工艺表里凡是写了关键设备的行，就是一道工序**。
            # 有些工序的 CPP 是 `—`（拣选+清洗、抛光、外包装、包装），
            # 但**仍指定了设备**——它们同样是工序，不能因为"没参数"就丢掉。
            #
            # ⚠️ 与「找不到编号」的区别，是这里的核心难点：
            #      · `equip_cell` 里**根本没写编号** → 这行不是工艺工序，跳过（正常）
            #      · `equip_cell` 里写了编号、但**编号不存在** → 这是**错误**，
            #        必须让边建出来，交给 validate 抓。
            #   第一版把两者都 `continue` 掉了 → 悬空引用被静默吞掉 →
            #   校验报"✅ 通过"（实测：塞入 `TQ-01` 后照样绿灯，边数却少了 1）。
            #
            # 所以这里用**宽松的编号形态**（任意 `XX-NN` 样式）来判断"这行有没有写设备"，
            # 而**严格格式校验交给 validate**。宽进严出。
            looks_like_equip = bool(re.search(r"[A-Z]{2,3}-\d{2,3}", equip_cell))
            if not looks_like_equip:
                continue

            proc_id = f"{prod}·{name}"
            g.add_node(Node(id=proc_id, type="process", label=name,
                            attrs={"产品": prod, "关键工艺参数": cpp, "收率指标": yield_},
                            coord=f"[{coord_p}:工艺表:{name}]"))
            g.add_edge(Edge(src=prod, dst=proc_id, relation="生产工序",
                            coord=f"[{coord_p}:工艺表:{name}]"))

            # 严格编号（`EQ-XX-###`）正常成边；形态可疑的（如 `TQ-01`）也成边，
            # 但额外记一条错误——**绝不静默吞掉**。
            codes = EQUIP_CODE.findall(equip_cell)
            if not codes:
                for bad in set(re.findall(r"[A-Z]{2,3}-\d{2,3}", equip_cell)):
                    g.errors.append(
                        f"工艺表「{name}」({prod}) 引用的设备编号 `{bad}` "
                        f"不符合 EQ-<两位字母>-<三位数字> 格式"
                        f"（坐标 {coord_p}）。**不存在 `TQ-01` 这类简写。**")
                continue
            for code in codes:
                g.add_edge(Edge(
                    src=proc_id, dst=code, relation="关键设备",
                    attrs={"对成本影响": impact},
                    coord=f"[{coord_p}:工艺表:关键设备列:{name}→{code}]"))


def extract_incidents(g: Graph, workspace_root=None, as_of=None) -> None:
    """
    从维修历史表抽 设备→故障事件。
    ⚠️ 这是**抓 TQ-01 类错误的关键环节**：
       若维修记录里提到的设备在台账中不存在，校验阶段会报错。
    """
    p = _source(workspace_root, "wiki/equipment/设备台账与维修历史.md")
    coord_p = "wiki/equipment/设备台账与维修历史.md"
    name2code = {n.label: n.id for n in g.by_type("equipment")}

    def _strip(s: str) -> str:
        """去 markdown 强调与行内代码记号。"""
        return re.sub(r"[*`]", "", s).strip()

    for r in _rows(p):
        # 维修表：| 日期 | 设备 | 故障描述 | 维修费(元) | 停工(h) | 影响 |
        # ⚠️ 日期可能带粗体（`**2026-03**`）——那是 wiki 用来标"最值得注意"的方式。
        #    实测踩过：第一版正则 `^\d{4}-\d{2}$` 只吃裸日期，
        #    结果**恰好漏掉唯一一条 2026 年的记录**（3 月胶囊填充机故障），
        #    而那条正是六味地黄成本异动的官方根因。漏最要紧的那条。
        raw0 = r[0].strip()
        m = re.match(r"^\*{0,2}(\d{4}-\d{2})\*{0,2}$", raw0)
        if len(r) < 4 or not m:
            continue
        date = m.group(1)
        if as_of is not None and date > as_of:
            continue
        dev = _strip(r[1])
        fault = _strip(r[2])
        inc_id = f"{date}·{dev}·{fault}"
        if inc_id in g.nodes:
            continue
        g.add_node(Node(id=inc_id, type="incident", label=f"{date} {dev}：{fault}",
                        attrs={"日期": date, "设备": dev, "故障": fault,
                               "维修费(元)": _strip(r[3]) if len(r) > 3 else "",
                               "停工(h)": _strip(r[4]) if len(r) > 4 else "",
                               "影响": _strip(r[5]) if len(r) > 5 else ""},
                        coord=f"[{coord_p}:维修历史:{date}]"))

        # 设备 → 故障 的边：设备单元格里常直接写着编号（`EQ-JN-006`），优先用它，
        # 比按名称反查可靠（名称可能带规格后缀，如「颗粒分装机（DXDK-40VI）」）。
        codes = EQUIP_CODE.findall(r[1])
        if codes:
            code = codes[0]
            if code not in g.nodes:
                g.errors.append(f"维修记录 {date} 引用了未登记的设备编号「{code}」")
            else:
                g.add_edge(Edge(src=code, dst=inc_id, relation="发生故障",
                                coord=f"[{coord_p}:维修历史:{date}]"))
            continue

        # 回退：按名称反查
        code = next((c for nm, c in name2code.items()
                     if dev and (dev in nm or nm in dev)), None)
        if code:
            g.add_edge(Edge(src=code, dst=inc_id, relation="发生故障",
                            coord=f"[{coord_p}:维修历史:{date}]"))
        else:
            # ⚠️ 维修记录引用了台账里没有的设备 → 校验阶段报错。
            #    这正是 demo/graph_kg.py 里 `TQ-01` 那类错误的产生方式。
            g.errors.append(
                f"维修记录 {date} 提到的设备「{dev}」在设备台账中找不到对应编号。"
                f"这正是 `TQ-01` 那类错误的产生方式。")


def extract_anomalies(g: Graph, workspace_root=None, as_of=None) -> None:
    """
    从成本异动登记册的「官方异动清单」抽异动节点与 异动→产品 边。
    表头：| # | 波动事件 | 涉及产品 | 发生月份 | 官方根因 |
    """
    p = _source(workspace_root, "wiki/costs/成本异动登记册.md")
    coord_p = "wiki/costs/成本异动登记册.md"
    if not p.exists():
        g.warnings.append("找不到成本异动登记册，跳过异动清单")
        return
    for r in _rows(p):
        if len(r) < 5 or not r[0].isdigit():
            continue
        _, event, prod, month, cause = r[0], r[1], r[2], r[3], r[4]
        if as_of is not None:
            periods = re.findall(r"\d{4}-(?:0[1-9]|1[0-2])", month)
            if not periods:
                g.warnings.append(f"异动「{event}」期间无法核对，未加入截至 {as_of} 的图谱")
                continue
            if any(value > as_of for value in periods):
                continue
        an_id = f"异动{event}"
        g.add_node(Node(id=an_id, type="anomaly", label=event,
                        attrs={"涉及产品": prod, "发生月份": month, "官方根因": cause},
                        coord=f"[{coord_p}:1 官方异动清单:{event}]"))
        # 产品节点可能已在 extract_materials 建过；这里不覆盖
        if prod and not prod.startswith("全部"):
            g.add_node(Node(id=prod, type="product", label=prod,
                            coord=f"[{coord_p}:1 官方异动清单:{prod}]"))
            g.add_edge(Edge(src=an_id, dst=prod, relation="涉及产品",
                            attrs={"月份": month},
                            coord=f"[{coord_p}:1 官方异动清单:{event}→{prod}]"))


def extract_peers(g: Graph, workspace_root=None) -> None:
    """对标单位节点 + 一厂/二厂 的同产品对比关系。"""
    g.add_node(Node(id=PEER, type="peer", label=PEER,
                    coord="[中药二厂_成本汇总_2026年1-6月.csv:工厂:中药二厂]"))
    f = _source(workspace_root, _PEER_FILE)
    if not f.exists():
        return
    with open(f, encoding="utf-8-sig", newline="") as fh:
        prods = {(r.get("产品名称") or "").strip() for r in csv.DictReader(fh)}
    for prod in sorted(p for p in prods if p):
        if prod in g.nodes:
            g.add_edge(Edge(
                src=prod, dst=PEER, relation="对标对比",
                coord=(f"[中药一厂_成本汇总_2026年1-6月.csv] + "
                       f"[中药二厂_成本汇总_2026年1-6月.csv:{prod}]")))


# ---------------------------------------------------------------------------
# 结构校验 ⭐
# ---------------------------------------------------------------------------

def validate(g: Graph, incident_minimum: int = 4) -> None:
    """
    结构校验：图的**引用完整性**。

    这是图谱相对普通文档的**最大增量价值**——文档里写错一个编号要靠人眼发现，
    图里引用一个不存在的节点是**可机械检测**的。
    """
    before = len(g.errors)

    # 1. 所有边的 src/dst 必须存在
    for e in g.edges:
        for end, tag in ((e.src, "src"), (e.dst, "dst")):
            if end not in g.nodes:
                g.errors.append(
                    f"边 {e.relation} 的 {tag} 节点「{end}」不存在"
                    f"（坐标 {e.coord or '无'}）")

    # 2. 设备编号格式校验：必须是 EQ-<两位大写字母>-<三位数字>
    for n in g.by_type("equipment"):
        if not re.fullmatch(r"EQ-[A-Z]{2}-\d{3}", n.id):
            g.errors.append(
                f"设备编号「{n.id}」不符合 EQ-<类别>-<三位流水> 格式"
                f"（不能是 TQ-01 这类简写）")

    # 3. 工序→设备 的引用必须是已登记设备
    for e in g.edges:
        if e.relation == "关键设备" and e.dst not in g.nodes:
            g.errors.append(
                f"工艺表引用了未登记的设备「{e.dst}」（坐标 {e.coord}）")

    # 4. 产品必须至少有一条处方原料边与一条工序边（否则抽取漏了）
    for n in g.by_type("product"):
        if not g.out(n.id):
            g.warnings.append(f"产品「{n.id}」没有任何出边——抽取可能不全")

    # 5. 孤立节点（既无出边也无入边）
    for n in g.nodes.values():
        if not g.out(n.id) and not g.inc(n.id):
            g.warnings.append(f"孤立节点：{n.type} / {n.id}")

    # 6. ⭐ **覆盖度断言**：抽取量不得低于已知基线。
    #
    #    为什么需要这条：第 1 条只能查"边的端点不存在"，
    #    **查不出"边根本没被创建"**——而后者才是本模块最容易犯的错
    #    （实测踩过：一条 continue 把带错误编号的工序整行吞掉，
    #      校验器看不到任何悬空引用，于是**假通过**、还报绿灯）。
    #    数量断言是这类静默丢失的兜底：**丢东西就会跌破基线。**
    BASELINE = {"equipment": 29, "incident": incident_minimum, "process": 20, "material": 15,
                "product": 3}
    for t, low in BASELINE.items():
        got = len(g.by_type(t))
        if got < low:
            g.errors.append(
                f"覆盖度不足：{t} 只抽到 {got} 个，低于基线 {low}。"
                f"**有内容被静默丢弃**，请检查抽取逻辑（不要用 continue 吞掉异常行）。")

    # 抽取与结构检查均无错误才显示通过，不能用本轮未新增掩盖已有错误。
    if not g.errors:
        g.warnings.insert(0, "✅ 结构校验通过：无悬空引用、设备编号格式合规、"
                             "各类节点数不低于基线")
    else:
        g.warnings.insert(0, f"❌ 图谱校验未通过：共有 {len(g.errors)} 处问题，"
                             f"其中结构检查新增 {len(g.errors) - before} 处，见 errors")


# ---------------------------------------------------------------------------
# 因果穿透
# ---------------------------------------------------------------------------

def trace(g: Graph, start: str, max_hops: int = 6) -> List[List[Edge]]:
    """
    从起点做**多跳正向前穿透**，返回若干条链路。

    这就是图谱相对「段落检索」的核心增量：
    检索只能找到「提到设备故障的那段文字」，
    穿透能回答「成本上涨**经过哪些环节**」——一条路径，而不只是一段话。
    """
    paths: List[List[Edge]] = []

    def dfs(node: str, path: List[Edge], seen: Set[str]) -> None:
        if len(path) >= max_hops or len(paths) >= 30:
            return
        outs = g.out(node)
        if not outs:
            if path:
                paths.append(list(path))
            return
        for e in outs:
            if e.dst in seen:
                continue
            path.append(e)
            dfs(e.dst, path, seen | {e.dst})
            path.pop()

    if start in g.nodes:
        dfs(start, [], {start})
    return paths


# ---------------------------------------------------------------------------

def build(verbose: bool = True, workspace_root=None, as_of=None) -> Dict[str, Any]:
    root = _workspace(workspace_root)
    signature = input_signature(root)
    period = _as_of(root, as_of)
    g = Graph()
    extract_equipment(g, root)
    extract_materials(g, root, period)
    extract_processes(g, root)
    extract_incidents(g, root, period)
    extract_anomalies(g, root, period)
    extract_peers(g, root)
    equipment_source = _source(root, "wiki/equipment/设备台账与维修历史.md")
    eligible_incidents = sum(1 for row in _rows(equipment_source)
                             if len(row) >= 4 and _MONTH.fullmatch(row[0].strip("*"))
                             and row[0].strip("*") <= period)
    validate(g, incident_minimum=eligible_incidents)
    if input_signature(root) != signature:
        raise ValueError("图谱输入在构建期间变化；未保存，请按当前资料重新构建")

    doc = g.to_dict()
    doc.update(schema_version=SCHEMA_VERSION, source_signature=signature,
               source_scope="authorized_workspace", as_of=period, saved=False)
    if not g.errors:
        target = _source(root, "graph/graph.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            doc["saved"] = True
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             prefix=".graph-", suffix=".tmp", dir=target.parent,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(doc, stream, ensure_ascii=False, indent=1)
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    if verbose:
        st = doc["stats"]
        print(f"图谱{'已建立' if doc['saved'] else '未保存'}：{st['nodes']} 节点 / {st['edges']} 边；截至 {period}")
        print(f"  节点类型: {st['by_type']}")
        print(f"  边类型  : {st['by_relation']}")
        for w in doc["warnings"][:6]:
            print(f"  ⚠️  {w}")
        for e in doc["errors"][:8]:
            print(f"  ❌ {e}")
    return doc


def _print_trace(g: Graph, start: str) -> None:
    paths = trace(g, start)
    node = g.nodes.get(start)
    print("=" * 76)
    print(f"因果穿透：{start}" + (f"（{node.type} · {node.label}）" if node else ""))
    print("=" * 76)
    if not paths:
        print("  无可穿透路径")
        return
    # 只显示最长的几条（最短的通常是"产品→原料"这类单跳）
    paths.sort(key=len, reverse=True)
    for i, p in enumerate(paths[:6], 1):
        chain = [start]
        for e in p:
            chain.append(f"-[{e.relation}]→")
            chain.append(e.dst)
        print(f"\n  路径 {i}（{len(p)} 跳）")
        print("    " + " ".join(chain))
        for e in p:
            if e.coord:
                print(f"      ↳ {e.relation}: {e.coord}")


def main() -> int:
    ap = argparse.ArgumentParser(description="因果知识图谱（派生视图）")
    ap.add_argument("--json", action="store_true", help="打印全图 JSON")
    ap.add_argument("--trace", help="从某节点做多跳因果穿透")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--wiki-root", type=Path, help="显式数据工作区根目录")
    ap.add_argument("--as-of", help="图谱截至月份 YYYY-MM；默认材料输入的最新月份")
    a = ap.parse_args()
    workspace_root = a.wiki_root
    doc = build(workspace_root=workspace_root, as_of=a.as_of)
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    elif a.trace:
        g = Graph()
        g.nodes = {item["id"]: Node(**item) for item in doc["nodes"]}
        g.edges = [Edge(**item) for item in doc["edges"]]
        _print_trace(g, a.trace)
    return 1 if doc["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
