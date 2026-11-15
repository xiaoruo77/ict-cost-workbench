"""Local evidence context for explanations; performs no model or network calls."""
from __future__ import annotations
import re
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PRODUCTS = ("银黄口服液", "板蓝根颗粒", "六味地黄胶囊")


def _admissible(line: str, product: str, month: str) -> bool:
    # A source may describe the whole semester. Do not leak future records into
    # a historical explanation, nor copy another product's incident.
    if any(p in line and p != product for p in PRODUCTS):
        return False
    # Remove source *filenames*, not bracketed facts or row coordinates. A
    # semester filename contains "1-6月", while (2026-06) can be a real event.
    temporal = re.sub(r"[\w一-鿿-]+\.(?:csv|pdf|docx|md|txt)", "", line)
    if any(m > month for m in re.findall(r"20\d{2}-\d{2}(?!\d)", temporal)):
        return False
    if month.startswith("2026-"):
        if any(int(m) > int(month[5:]) for m in re.findall(r"(?<!\d)(1[0-2]|[1-9])月", temporal)):
            return False
        names = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
                 "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
        if any(names[m] > int(month[5:]) for m in re.findall(r"(十二|十一|[一二三四五六七八九十])月", temporal)):
            return False
    return True


def evidence_records(product: str, month: str, max_chars: int = 6500,
                     workspace_root: Path | None = None) -> list[dict]:
    if product not in PRODUCTS or not re.fullmatch(r"\d{4}-\d{2}", month):
        return []
    paths = [f"products/{product}.md", "costs/成本异动登记册.md",
             "costs/成本数据口径与结构.md", "gmp/GMP质量约束与成本关联.md"]
    records = []
    total_used = 0
    root = Path(workspace_root) if workspace_root is not None else ROOT
    from overlay import contained_path
    for relative in paths:
        path = contained_path(root / "wiki", relative)
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        kept, headings = [], []
        for line_number, line in enumerate(lines, 1):
            heading = re.match(r"^(#{1,6})\s+", line)
            if heading:
                level = len(heading[1])
                headings = headings[:level - 1] + [line]
            if line.startswith(("> 所属", "> 最后编译", "> 信息截点")):
                continue
            if not all(_admissible(h, product, month) for h in headings):
                continue
            if not _admissible(line, product, month):
                continue
            if relative == "costs/成本异动登记册.md":
                # Shared incident prose without explicit scope is never a
                # causal input. Include only rows scoped to this product/month.
                if not (line.lstrip().startswith("|") and product in line and month in line):
                    continue
            elif relative.startswith(("costs/", "gmp/")):
                if not (line.startswith(("#", ">")) or any(
                        word in line for word in ("争议", "缺失", "不能", "不得", "禁止", "需核查", "未提供", "场景设定", "审批", "核实"))):
                    continue
            kept.append((line_number, line))
        # Put limitations/Status near the beginning so truncation cannot silently
        # remove them after a large numerical table.
        caution = [pair for pair in kept if pair[1].startswith(">") or
                   any(w in pair[1] for w in ("争议", "缺失", "不能", "需核查", "未提供", "场景设定"))]
        detail = [pair for pair in kept if pair not in caution]
        used = 0
        for line_number, line in caution + detail:
            if not line.strip() or used + len(line) > 1400 or total_used + len(line) > max_chars:
                continue
            identity = f"wiki/{relative}:L{line_number}:{line}"
            records.append({"id": "evidence:" + hashlib.sha256(identity.encode()).hexdigest()[:16],
                            "path": "wiki/" + relative, "line": line_number, "text": line,
                            "product": product, "as_of": month, "kind": "background_record"})
            used += len(line) + 1
            total_used += len(line) + 1
    return records


def context_for(product: str, month: str, max_chars: int = 6500,
                workspace_root: Path | None = None) -> str:
    boundary = ("【证据使用边界】以下为本地背景记录，不代表已证实因果。"
                "工艺定额与配方不能冒充本期实际；具体原因需采购合同、用量及批次记录核查。"
                "禁止降低检验、改变验证周期或未经批准返工。")
    records = evidence_records(product, month, max_chars, workspace_root)
    return boundary + "\n" + ("\n".join(
        f"[{item['id']} {item['path']}:L{item['line']}] {item['text']}" for item in records)
        if records else "尚无符合产品和截至期范围的知识证据，具体原因需进一步核查。")


def fact_records(fs) -> dict[str, dict]:
    """Bind identity, signed value, period and provenance before model writing."""
    records = {}
    for label, fact in fs.facts.items():
        if fact.value is None:
            continue
        period = fact.period or fs.month
        shown_label = label
        if getattr(fs, "period_kind", "monthly") == "quarterly":
            shown_label = label.replace("本月", "本季度").replace("上月", "上季度")
            year, quarter = fs.period_label.split("-Q")
            if label.startswith("上月") or label.endswith("上月"):
                period = f"{int(year)-1}-Q4" if quarter == "1" else f"{year}-Q{int(quarter)-1}"
            elif label.startswith("去年"):
                period = f"{int(year)-1}-Q{quarter}"
            else:
                period = fs.period_label
        item = {"product": fs.product, "period": period, "metric": shown_label,
                "value": fact.value, "display": fact.display, "unit": fact.unit,
                "coord": fact.coord, "sources": fact.input_sources,
                "formula": fact.formula, "source_kind": fact.source_kind}
        identity = json.dumps(item, ensure_ascii=False, sort_keys=True)
        item["id"] = "fact:" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        records[item["id"]] = item
    return records


def validate_qualitative(text: str, product: str, month: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("叙述的待核查文本必须非空")
    if re.search(r"\d|[①-⑳]|百分之[零〇一二三四五六七八九十百千万亿点两]+|"
                 r"[零〇一二三四五六七八九十百千万亿两]+(?:元|倍|个百分点|月|年|盒|吨|千克)", text):
        raise ValueError("自由叙述不得写数值或期间；必须选择绑定的 fact_id")
    if not _admissible(text, product, month):
        raise ValueError("叙述的产品或截至期范围不符")
    if re.search(r"已证实|已经证实|确定(?:是|由|导致)|根本原因|主要原因(?:是|为)|必然|导致|造成|归因于|同比|环比", text):
        raise ValueError("自由叙述包含未经绑定的事实或确定性因果")
    if re.search(r"降低检验|减少检验|延长验证周期|无需审批|直接返工", text):
        raise ValueError("建议不符合质量约束")
    return text.strip()


def render_grounded_field(item: dict, facts: dict, evidence: dict,
                          product: str, month: str) -> str:
    """Only deterministic blocks assert facts; generated prose remains a hypothesis."""
    required = {"fact_ids", "evidence_ids", "hypotheses", "checks"}
    if not isinstance(item, dict) or set(item) != required:
        raise ValueError("叙述必须返回 fact_ids/evidence_ids/hypotheses/checks 结构")
    for key in required:
        if not isinstance(item[key], list) or any(not isinstance(value, str) for value in item[key]):
            raise ValueError("叙述证据字段必须为字符串数组")
        if len(item[key]) > 3 or len(item[key]) != len(set(item[key])):
            raise ValueError("叙述证据引用数量过多或重复")
    parts = []
    for identity in item["fact_ids"]:
        if identity not in facts:
            raise ValueError("叙述引用未知或不属于本次快照的fact_id")
        fact = facts[identity]
        if fact["product"] != product:
            raise ValueError("叙述事实对象不一致")
        unit = fact["unit"] if not fact["display"].endswith(fact["unit"] or "\0") else ""
        source = fact["coord"] or ("期间聚合，见来源附录" if fact["sources"] else fact["formula"] or "来源待补")
        parts.append(f"{fact['product']} {fact['period']} · {fact['metric']}：{fact['display']}{unit}（{source}）。")
    for identity in item["evidence_ids"]:
        if identity not in evidence:
            raise ValueError("叙述引用未知或超出产品/截至期的evidence_id")
        record = evidence[identity]
        if record["product"] != product or record["as_of"] != month:
            raise ValueError("背景证据范围不一致")
        parts.append(f"背景记录（不能单独证明本期原因）：{record['text']} [{record['path']}:L{record['line']}]。")
    for text in item["hypotheses"]:
        parts.append("待核查假设：" + validate_qualitative(text, product, month) + "。")
    for text in item["checks"]:
        parts.append("建议核查：" + validate_qualitative(text, product, month) + "。")
    if not parts:
        raise ValueError("叙述不得返回空字段")
    return "\n".join(parts)
