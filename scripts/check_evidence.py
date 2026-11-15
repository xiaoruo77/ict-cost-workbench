#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Legacy compatibility implementation; original source materials are not included."""














































import re
import sys
import io
from pathlib import Path

# Windows 控制台中文兼容
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

NUMBER_TOKEN_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)*(?:\s*[KMB%](?![A-Za-z]))?"
    r"|\d+(?:\.\d+)+[KMB%]?"
    r"|\d+(?:\.\d+)*\s*[KMB%](?![A-Za-z])"
    r"|\d{4,})"
    r"(?![0-9])"
)
DATE_RE = re.compile(r"\d{4}-\d{2}(?:-\d{2})?")
QUOTE_RES = [re.compile(r'"([^"\n]{15,})"'), re.compile(r"“([^”\n]{15,})”")]
# 元数据键：英文（Karpathy 原始范式）+ 中文（本库词条头）
METADATA_RE = re.compile(
    r"^>\s*(Sources?|Raw|Collected|Published|Updated|Archived"
    r"|所属分类|实体类型|信息截点|信源摘要|最后编译|采集日期|归档日期|引用|归档)\s*[:：]"
)
STATUS_LINE_RE = re.compile(r"^>\s*\*\*Status:")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
RAW_LINK_RE = re.compile(r"\(([^)]+)\)")
NO_MATERIAL_RE = re.compile(r"ingest\s*\|\s*no material:\s*(\S+)", re.I)
HEADING_RE = re.compile(r"^#{1,6}\s")


def read(p: Path) -> str:
    """读文本内容。若目标是 .pdf，改读其同名 .txt 派生缓存——
    PDF 是权威件但二进制不可 grep，.txt 正是为此而存在（见 SKILL.md）。"""
    if p.suffix.lower() == ".pdf":
        sib = p.with_suffix(".txt")
        if sib.exists():
            return sib.read_text(encoding="utf-8", errors="replace")
    return p.read_text(encoding="utf-8", errors="replace")


def split_metadata(text: str):
    """返回 (raw_links, body)。metadata 行是以 `>` 开头的键值行。"""
    raw_links, body = [], []
    for line in text.split("\n"):
        if METADATA_RE.match(line):
            if line.strip().startswith("> Raw:") or line.strip().startswith(">Raw:"):
                raw_links += [m.group(1) for m in RAW_LINK_RE.finditer(line)]
            continue
        body.append(line)
    return raw_links, "\n".join(body)


def write_raw_index(root: Path) -> dict:
    """raw/ 下所有文件名 → 相对路径。用于按名回退匹配。"""
    idx = {}
    for f in (root / "raw").rglob("*"):
        if f.is_file():
            idx.setdefault(f.name, []).append(f)
    return idx


def strip_noise(text: str) -> str:
    """去掉行内代码、链接目标，避免把代码/URL 当事实候选。"""
    text = INLINE_CODE_RE.sub(" ", text)
    return text


def main():
    args = [a for a in sys.argv[1:]]
    root = Path(args[0]).resolve() if args and Path(args[0]).is_dir() else Path.cwd()
    wiki = root / "wiki"
    if not wiki.exists():
        print(f"找不到 wiki/ 于 {root}")
        return 1

    explicit = [a for a in args if a.endswith(".md")]
    if explicit:
        articles = [(root / p).resolve() for p in explicit]
    else:
        articles = [p for p in sorted(wiki.rglob("*.md"))
                    if p.name not in ("index.md", "log.md")]

    raw_idx = write_raw_index(root)
    log_text = read(wiki / "log.md") if (wiki / "log.md").exists() else ""
    no_material = {m.group(1).split("/")[-1] for m in NO_MATERIAL_RE.finditer(log_text)}

    referenced, fidelity, evid_err, unresolved = set(), [], [], []
    for art in articles:
        rel = art.relative_to(root).as_posix()
        text = read(art)
        raw_links, body = split_metadata(text)

        if not raw_links:
            evid_err.append((rel, "缺 Raw 字段"))
            continue

        # 解析 raw 链接
        src_texts = []
        for lk in raw_links:
            tgt = (art.parent / lk).resolve()
            if not tgt.exists():
                # 按文件名回退
                cands = raw_idx.get(Path(lk).name, [])
                if len(cands) == 1:
                    tgt = cands[0]
                else:
                    unresolved.append((rel, lk, len(cands)))
                    continue
            if "raw" not in tgt.parts:
                evid_err.append((rel, f"Raw 链接逃出 raw/: {lk}"))
                continue
            referenced.add(tgt.name)
            src_texts.append(read(tgt))

        if not src_texts:
            evid_err.append((rel, "Raw 链接全部不可解析"))
            continue

        blob = "\n".join(src_texts)
        clean = strip_noise(body)

        cands = [(m.group(0), "number") for m in NUMBER_TOKEN_RE.finditer(clean)]
        cands += [(m.group(0), "date") for m in DATE_RE.finditer(clean)]
        for qr in QUOTE_RES:
            cands += [(m.group(1), "quote") for m in qr.finditer(clean)]

        seen = set()
        for tok, kind in cands:
            if tok in seen:
                continue
            seen.add(tok)
            # 归一化：去千分位、去空格、去尾零（补零允许，截断仍会命中）
            variants = {tok, tok.replace(",", ""), tok.replace(" ", "")}
            if "." in tok:
                base = tok.replace(",", "")
                m = re.match(r"^(\d+\.\d+?)(0+)(\D*)$", base)
                if m:
                    variants.add(m.group(1) + m.group(3))
                    variants.add(m.group(1).rstrip("0").rstrip(".") + m.group(3))
            if not any(v in blob for v in variants):
                fidelity.append((rel, kind, tok))

    # ---- 结构检查 ----
    idx_text = read(wiki / "index.md") if (wiki / "index.md").exists() else ""
    missing_in_index = [a.relative_to(root).as_posix() for a in articles
                        if Path(a.relative_to(root).as_posix()).name not in idx_text]

    dead_links = []
    for art in articles + [wiki / "index.md"]:
        if not art.exists():
            continue
        rel = art.relative_to(root).as_posix()
        for m in LINK_RE.finditer(read(art)):
            href = m.group(2)
            if href.startswith(("http", "#", "mailto")) or not href.endswith(".md"):
                continue
            if not (art.parent / href).resolve().exists():
                dead_links.append((rel, href))

    all_raw = {f.name for f in (root / "raw").rglob("*") if f.is_file()
               and f.suffix in (".csv", ".pdf", ".md", ".py", ".docx")}
    unreferenced = sorted(all_raw - referenced - no_material)

    # ---- 数值快照同步检查 ----
    # state/metrics.json 是派生视图；它陈旧时，报告侧会取到过期值。
    # 这里只**报**，不自动重建（重建要写文件，属于"只报不改"之外的动作）。
    state_info = None
    try:
        import state as _S
        v = _S.verify(wiki)
        state_info = v
    except Exception as e:  # noqa: BLE001
        state_info = {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    # ---- 报告 ----
    R = "=" * 66
    print(f"{R}\n机械证据校验报告  root={root}\n{R}")
    print(f"文章 {len(articles)} 篇 | 溯源文件 {len(referenced)} 个 | 权威 raw {len(all_raw)} 个\n")

    print(f"[1] 结构：索引缺失 {len(missing_in_index)}，死链 {len(dead_links)}")
    for r in missing_in_index:
        print(f"     索引未收录: {r}")
    for a, h in dead_links:
        print(f"     死链: {a} → {h}")

    print(f"\n[2] 证据错误（需人决定）: {len(evid_err)}")
    for a, why in evid_err:
        print(f"     {a}: {why}")

    print(f"\n[3] Raw 链接不可解析: {len(unresolved)}")
    for a, lk, n in unresolved:
        print(f"     {a}: {lk} ({n} 个同名候选)")

    print(f"\n[4] 来源保真可疑点（候选，非判决）: {len(fidelity)}")
    for a, kind, tok in fidelity[:40]:
        print(f"     [{kind}] {tok:<22} ← {a}")
    if len(fidelity) > 40:
        print(f"     ... 另有 {len(fidelity)-40} 条")

    print(f"\n[5] 数值快照（state/metrics.json）同步状态")
    si = state_info or {"ok": False, "reason": "未检查"}
    if si.get("reason"):
        print(f"     ⚠️  {si['reason']}")
        print("        → 快照不可用。报告侧应改用实时检索，或跑 "
              "`python scripts/state.py --build`")
    elif si.get("ok"):
        print(f"     ✅ 与 wiki 同步（签名 {si.get('wiki_signature')}）")
    else:
        print("     ⚠️  **已过期** —— 快照与 wiki 不一致，报告侧可能取到旧值")
        if si.get("changed"):
            print(f"        值变化 {len(si['changed'])} 条：")
            for c in si["changed"][:8]:
                print(f"           {c['id']}  {c['was']} → {c['now']}")
            if len(si["changed"]) > 8:
                print(f"           … 另有 {len(si['changed']) - 8} 条")
        if si.get("added"):
            print(f"        新增 {len(si['added'])} 条")
        if si.get("removed"):
            print(f"        消失 {len(si['removed'])} 条")
        print("        → 跑 `python scripts/state.py --build` 重建")

    print(f"\n[6] 未被任何文章引用的 raw 文件: {len(unreferenced)}")
    for f in unreferenced:
        print(f"     {f}")

    # ---- [7] 换行符卫生 ------------------------------------------------
    #
    # 为什么需要这一节：本库实际踩过一次**静默**的换行符污染——
    # 编辑工具用 `write_text()`（默认 newline=None）写文件，Windows 上把 `\n`
    # 翻成 `\r\n`，而读取是裸 decode 不做逆翻译，于是**每次"读→改→写"多留一个 `\r`**。
    # 一次摄入做 9 次 patch 后，195 行的词条在 `\n` 视角下变成 1300 行，
    # `state.py` 的表格解析整段失效，关键数值 **555 → 339，掉了 216 条**。
    #
    # ⚠️ 危险点在**静默**：不报错、文件能打开、肉眼看着正常。
    #    只有 diff 和抽数会异常。所以这里把它变成**响亮的失败**。
    #
    # 判定：CRLF 单独出现是平台差异（容忍）；但 `\r\r`（乘性重复）一定是 bug。
    crlf_files, cr_crlf_files = [], []
    for p in sorted(list((root / "wiki").rglob("*.md")) + [wiki / "index.md", wiki / "log.md"]):
        if not p.exists():
            continue
        b = p.read_bytes()
        if b.count(b"\r") == 0:
            continue
        rel = p.relative_to(root).as_posix()
        if re.search(rb"\r\r", b):
            cr_crlf_files.append(rel)
        else:
            crlf_files.append(rel)

    print(f"\n[7] 换行符卫生：乘性污染 {len(cr_crlf_files)}，普通 CRLF {len(crlf_files)}")
    for f in cr_crlf_files:
        print(f"     ❌ {f}  （含 `\\r\\r`，写入未钉死 newline=\"\\n\"；"
              f"会静默破坏 state 抽数）")
    if cr_crlf_files:
        print("     → 修复：`python scripts/fix_newlines.py`（先 --check）")
    for f in crlf_files[:5]:
        print(f"     · {f}（仅 CRLF，平台差异，可忽略）")
    if len(crlf_files) > 5:
        print(f"     · … 另有 {len(crlf_files) - 5} 个")

    print(f"\n{R}\n退出码不承载信息；请人工判读以上清单。\n{R}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
