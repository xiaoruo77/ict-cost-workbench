# -*- coding: utf-8 -*-
"""Deterministic request routing and workspace-bound fact answers.

Current deployment uses local BM25 / state for retrieval and only deepseek-flash
when explicitly requested. Legacy small/large labels describe request complexity,
not different models. Numeric membership alone is not evidence of correct entity,
period, or cause; rendered facts therefore bind all three to a stable fact ID.
"""

from __future__ import annotations
from model_policy import require_flash

import argparse
import json
import os
import re
import sys
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

SMALL_MODEL_ENV = "SILICONFLOW_MODEL"
SMALL_MODEL_DEFAULT = "deepseek-flash"
BIG_MODEL_ENV = "AGENT_MODEL"
BIG_MODEL_DEFAULT = "deepseek-flash"

# 决策档位
L_STATE, L_SMALL, L_LARGE = "state", "small", "large"
_WORKSPACE = ContextVar("router_workspace", default=None)
_VERIFIED_DOC = ContextVar("router_verified_doc", default=None)
_SCOPED = ContextVar("router_scoped", default=False)


@contextmanager
def _scope(workspace_root=None):
    selected = Path(workspace_root).resolve() if workspace_root is not None else _WORKSPACE.get()
    same_scope = _SCOPED.get() and selected == _WORKSPACE.get()
    token = _WORKSPACE.set(selected)
    doc_token = _VERIFIED_DOC.set(_VERIFIED_DOC.get() if same_scope else None)
    scope_token = _SCOPED.set(True)
    try:
        yield
    finally:
        _SCOPED.reset(scope_token)
        _VERIFIED_DOC.reset(doc_token)
        _WORKSPACE.reset(token)


# =============================================================================
# 一、确定性信号
#
# 每个信号只回答一个是非/计数问题，全部可复算。**不用 LLM 判意图。**
# =============================================================================

# 写入类动词 —— 会改 wiki/raw 或产出交付物
WRITE_WORDS = (
    "生成报告", "出报告", "导出", "写进", "入库", "更新知识库", "新增词条",
    "编译", "摄入", "派发", "下发", "整改任务", "生成任务", "重新生成",
    "更新看板", "存档",
)
# 需要推理的词 —— 问"为什么/趋势/建议"的，小模型答不好
REASON_WORDS = (
    "为什么", "原因", "归因", "分析", "对比", "比较", "差异", "趋势",
    "建议", "评价", "影响", "预测", "判断", "怎么", "如何",
)

TIME_RE = re.compile(r"20\d{2}[-/年]?\d{1,2}(?:[-/月])?")
NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


@dataclass
class Signal:
    """一个路由信号的判定结果。"""
    name: str
    value: Any
    why: str

    def __str__(self) -> str:
        return f"{self.name}={self.value}（{self.why}）"


def _db_path() -> Path:
    import paths
    workspace = _WORKSPACE.get()
    return workspace / "state" / "knowledge.db" if workspace is not None else paths.knowledge_db()


@contextmanager
def _connect():
    """
    以**只读**方式打开知识库镜像。

    ⚠️ `mode=ro` 不是可选的严谨，而是职责约束：取数层**只读**。
       写库的只有 `db_build.py`（它自己重建镜像）。
    """
    import sqlite3
    p = _db_path()
    if not p.exists():
        raise FileNotFoundError(
            f"知识库镜像不存在：{p}\n  先跑：python scripts/db_build.py")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        with con:
            yield con
    finally:
        con.close()


def db_ready() -> Tuple[bool, str]:
    """
    库在不在、**新不新鲜**。

    ⚠️ 过期校验不能省：`mirror_metrics` 是从 `state/metrics.json` 镜像来的，
       而 state 又是从 wiki 蒸馏的。wiki 改了、库没重建，库里就是**旧值**——
       它看起来完好无损，却和当前知识库对不上。
       **"正确但过时"是最难发现的错**，所以这里机械比对签名，过期就拒绝用。
    """
    import state as S
    if _SCOPED.get() and _VERIFIED_DOC.get() is not None:
        return True, ""
    workspace = _WORKSPACE.get() or WIKI_ROOT
    doc = S.load(workspace_root=workspace)
    if not doc:
        return False, "当前工作区没有数值快照，请先编译当前用户资料"
    if not S.verify(workspace / "wiki", workspace_root=workspace, doc=doc).get("ok"):
        return False, "当前工作区的wiki已变化，数值快照已过期；拒绝引用旧数值"
    if _WORKSPACE.get() is not None:
        if _SCOPED.get():
            _VERIFIED_DOC.set(doc)
        return True, ""
    p = _db_path()
    if not p.exists():
        return False, f"知识库镜像不存在（跑 python scripts/db_build.py）"
    try:
        import state as S
        with _connect() as con:
            row = con.execute(
                "SELECT value FROM meta WHERE key='wiki_signature'").fetchone()
        db_sig = (row["value"] if row else "") or ""
        cur_sig = doc.get("wiki_signature", "")
        if not db_sig or not cur_sig or db_sig != cur_sig:
            return False, (f"知识库镜像已过期（库 {db_sig[:8]} ≠ 当前 {cur_sig[:8]}）"
                           f"——重建：python scripts/db_build.py")
        if _SCOPED.get():
            _VERIFIED_DOC.set(doc)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"知识库镜像不可用：{type(e).__name__}: {e}"


def _db_rows(entity: str = "", metric: str = "", period: str = "",
             limit: int = 200) -> List[Dict[str, Any]]:
    """
    从 `mirror_metrics` 取数（子串匹配，与 `state.lookup` 语义一致）。

    ⚠️ 刻意**对齐 state.lookup 的语义**（子串、空条件不限），
       这样换成读库不会悄悄改变行为——只是换了数据源，判定逻辑不变。
    """
    if _SCOPED.get():
        ready, reason = db_ready()
        if not ready:
            raise ValueError(reason)
        import state as S
        return [dict(row, source="personal" if _WORKSPACE.get() is not None else "shared")
                for row in S.lookup(entity, metric, period, doc=_VERIFIED_DOC.get())[:limit]]
    if _WORKSPACE.get() is not None:
        import state as S
        doc = S.load(workspace_root=_WORKSPACE.get())
        if not S.verify(workspace_root=_WORKSPACE.get(), doc=doc).get("ok"):
            raise ValueError("当前工作区数值快照未验证或已过期")
        return [dict(row, source="personal") for row in S.lookup(entity, metric, period, doc=doc)[:limit]]
    sql = ("SELECT entity,metric,period,value,unit FROM mirror_metrics WHERE 1=1")
    args: List[Any] = []
    if entity:
        sql += " AND entity LIKE ?"
        args.append(f"%{entity}%")
    if metric:
        sql += " AND metric LIKE ?"
        args.append(f"%{metric}%")
    if period:
        sql += " AND period LIKE ?"
        args.append(f"%{period}%")
    sql += " LIMIT ?"
    args.append(limit)
    with _connect() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


@dataclass
class Vocab:
    ent: set
    met: set
    per: set


def _state_vocab() -> Vocab:
    """
    从**数据库**里取三套词表：实体 / 指标 / 期间。

    ⚠️ 只做"词表"，不做语义理解——子串匹配本就是取数层的语义。
       从 DB 取而不是扫 JSON：库上有 `idx_met_em` / `idx_met_per` 索引，
       且 DISTINCT 在 SQLite 里比在 Python 里集合去重省内存。
    """
    rows = _db_rows(limit=100000)
    ent = {r.get("entity") for r in rows if r.get("entity")}
    met = {r.get("metric") for r in rows if r.get("metric")}
    per = {r.get("period") for r in rows if r.get("period")}
    return Vocab(ent, met, per)


def _find_in(text: str, vocab: set) -> List[str]:
    """在提问里找出词表中出现的项（长的优先，避免"单位成本"被"成本"截断）。"""
    hits = [v for v in vocab if v and v in text]
    hits.sort(key=lambda value: (-len(value), value))
    # 去掉被更长命中包含的短项
    out: List[str] = []
    for h in hits:
        if not any(h != o and h in o for o in out):
            out.append(h)
    return out


def sig_write(question: str, **_k: Any) -> Signal:
    """信号①：**写入意图** —— 会不会改知识库 / 出交付物。"""
    hit = [w for w in WRITE_WORDS if w in question]
    if hit:
        return Signal("写入意图", True, f"命中写入类词：{'、'.join(hit)}")
    return Signal("写入意图", False, "无写入类词")


def _lookup(question: str, vocab: Optional[Vocab] = None,
            limit: int = 200) -> Tuple[List[Dict[str, Any]], List[str]]:
    """抽提问里的 实体/指标/期间，从库里查。返回 (行, 命中的词)。"""
    v = vocab or _state_vocab()
    e_hits = _find_in(question, v.ent)
    m_hits = _find_in(question, v.met)
    normalized = re.sub(r"(20\d{2})[-/年](\d{1,2})月?",
                        lambda m: f"{m.group(1)}-{int(m.group(2)):02d}", question)
    p_hits = _find_in(normalized, v.per)
    explicit_periods = re.findall(r"20\d{2}-\d{2}", normalized)
    if any(period not in v.per for period in explicit_periods):
        return [], [*e_hits, *m_hits, *explicit_periods]
    if not explicit_periods:
        month_numbers = {int(x) for x in re.findall(r"(?<!\d)(\d{1,2})月", question)}
        if month_numbers:
            p_hits = sorted(p for p in v.per if re.fullmatch(r"20\d{2}-\d{2}", p)
                            and int(p[-2:]) in month_numbers)
            if not p_hits:
                return [], [*e_hits, *m_hits]
    rows = [row for row in _db_rows(limit=100000)
            if (not e_hits or any(term in str(row.get("entity", "")) for term in e_hits))
            and (not m_hits or any(term in str(row.get("metric", "")) for term in m_hits))
            and (not p_hits or row.get("period") in p_hits)]
    rows.sort(key=lambda row: (str(row.get("period", "")), str(row.get("entity", "")),
                               str(row.get("metric", "")), str(row.get("article", "")), row.get("line", 0)))
    rows = rows[:limit]
    return rows, [*e_hits, *m_hits, *p_hits]


def sig_state_direct(question: str, *, vocab=None, **_k: Any) -> Signal:
    """
    信号②：**库能否直答** —— 命中**唯一**记录时，连模型都不用。

    ⚠️ 判据刻意保守：**只有恰好 1 条**才算能直答。
       命中多条说明有歧义（"人工"能匹配到几十条），那就该交给模型去读，
       不能脚本随便挑一条——**挑错了就是错数**。
    """
    v = vocab or _state_vocab()
    if not _find_in(question, v.met):
        return Signal("库直答", False, "没识别出指标")

    rows, _ = _lookup(question, v, limit=3)
    if len(rows) == 1:
        r = rows[0]
        return Signal("库直答", True,
                      f"唯一命中：{r['entity']}/{r['metric']}"
                      f"/{r['period']}={r['value']}")
    return Signal("库直答", False,
                  f"命中 {len(rows)} 条，有歧义" if rows else "无命中")


def sig_sources(question: str, **_k: Any) -> Signal:
    """信号③：**数据源数量** —— 单源给小模型，跨源交叉给大模型。

    确定性代理：提问里出现了**几个不同产品**。多产品要横向对比，
    小模型容易把两边的数串起来（本库最怕的"张冠李戴"）。
    """
    products = ("银黄口服液", "板蓝根颗粒", "六味地黄胶囊")
    hit = [p for p in products if p in question]
    n = len(hit)
    return Signal("数据源数", n,
                  f"提及 {'、'.join(hit)}" if hit
                  else "未点名产品（视作共享基线单源）")


def sig_output_type(question: str, **_k: Any) -> Signal:
    """信号④：**输出类型** —— 问答 / 报告 / RPA 任务。"""
    if any(w in question for w in ("报告", "导出", "PDF", "Word", "pdf", "word")):
        return Signal("输出类型", "report", "命中报告/导出类词")
    if any(w in question for w in ("整改", "派发", "下发", "任务", "工单")):
        return Signal("输出类型", "rpa", "命中整改/任务类词")
    return Signal("输出类型", "qa", "默认问答")


SIGNALS = (sig_write, sig_state_direct, sig_sources, sig_output_type)


# =============================================================================
# 二、路由
# =============================================================================

@dataclass
class Decision:
    level: str                                   # state | small | large
    reasons: List[Signal] = field(default_factory=list)
    question: str = ""
    payload: Any = None                          # state 直答时的命中行 / 小模型用的上下文

    @property
    def is_direct(self) -> bool:
        return self.level == L_STATE

    def explain(self) -> str:
        head = {L_STATE: "① 知识库直答（0 token）",
                L_SMALL: "② 单源事实查询（默认脚本，显式启用时仅Flash）",
                L_LARGE: "③ 复杂请求（仅Flash可选，事实仍由脚本校验）"}[self.level]
        lines = [f"裁决：{head}"]
        for s in self.reasons:
            lines.append(f"  · {s}")
        return "\n".join(lines)


def route(question: str, *, vocab=None, workspace_root=None) -> Decision:
    with _scope(workspace_root):
        return _route(question, vocab=vocab)


def _route(question: str, *, vocab=None) -> Decision:
    """
    确定性路由。规则按**风险从高到低**判——先排掉"必须用大模型"的，
    再看能不能省钱。

    ⚠️ 顺序很重要：写入类永远走大模型，**写权限不给弱模型**
       （小模型错误率更高，而写 wiki 是不可逆的，会污染唯一事实源）。
    """
    sigs: List[Signal] = [
        sig_write(question),
        sig_state_direct(question, vocab=vocab),
        sig_sources(question),
        sig_output_type(question),
    ]
    get = {s.name: s for s in sigs}

    # ① 写权限：大模型。没有例外。
    if get["写入意图"].value:
        return Decision(L_LARGE, sigs, question)

    # ② 交付物（报告 / RPA）：大模型。
    if get["输出类型"].value in ("report", "rpa"):
        return Decision(L_LARGE, sigs, question)

    # ③ 跨源交叉：大模型。
    if get["数据源数"].value >= 2:
        return Decision(L_LARGE, sigs, question)

    # ④ 需要推理：大模型（小模型答不好"为什么"）。
    if any(w in question for w in REASON_WORDS):
        return Decision(L_LARGE, sigs, question)

    # ⑤ 唯一命中：脚本直接答。
    if get["库直答"].value:
        rows, _ = _lookup(question, vocab, limit=3)
        return Decision(L_STATE, sigs, question, payload=rows[0] if rows else None)

    # ⑥ 其余：只读、单源、简单问答 → 小模型
    return Decision(L_SMALL, sigs, question)


# =============================================================================
# 三、模型调用
# =============================================================================

def _load_env() -> None:
    from dotenv import load_dotenv
    for p in [WIKI_ROOT, *WIKI_ROOT.parents]:
        env = p / ".env"
        if env.exists():
            load_dotenv(dotenv_path=env)
            return


def _client(kind: str):
    """Only the explicitly allowed Flash model; kind is a legacy API label."""
    import httpx
    from langchain_openai import ChatOpenAI

    _load_env()
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("未找到 DEEPSEEK_API_KEY（大模型不可用）")
    return ChatOpenAI(
        model=require_flash(os.getenv(BIG_MODEL_ENV, BIG_MODEL_DEFAULT)),
        api_key=key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        temperature=0.2,
        http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
        http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
    )


SMALL_SYSTEM = """你是中药一厂成本数据的查询助手。

**铁律（违反即不合格）：**
1. **你只能引用「参考数据」里出现过的数字。** 不许计算、不许推算、不许编造。
2. **只要参考数据里有相关记录，就必须把它们如实列出来**（按期间顺序），
   不要回答"没有该数值"。
3. **仅当参考数据里确实找不到相关记录时**，才回答「参考数据中没有该数值」。
4. 回答简短，直接列数值即可。

你只做**查询与转述**，不做分析、不做归因、不提建议。
"""


def build_context(question: str, *, vocab=None, limit: int = 6) -> str:
    """
    给**小模型**准备的极小上下文 —— **从数据库的关键数值表取**。

    ⚠️ 这是省钱的关键：只喂命中的那几条，**不喂整个 wiki / 不喂整个库**。
       小模型便宜在单价，喂全库一样会贵，而且更容易跑偏。

    取数顺序：
      ① 实体+指标+期间 一起匹配（最精确）
      ② 没结果就放宽成「只按指标」（实测多数提问只说指标，不说实体）
    """
    v = vocab or _state_vocab()
    rows, _ = _lookup(question, v, limit=limit)
    lines = [f"- {r['entity']} / {r['metric']} / {r['period']} = {r['value']}"
             f"{r.get('unit') or ''}" for r in rows[:limit]]
    return "\n".join(lines) if lines else "（无匹配记录）"


# =============================================================================
# 四、校验（小模型的唯一安全带）
# =============================================================================

def validate_numbers(answer: str, context: str) -> Tuple[bool, List[str]]:
    """
    答案里出现的**每个数字**，都必须在参考答案里能找到。

    ⚠️ 没有这道校验，小模型就是纯风险：省下的 token 换来的是**没人发现的错数**。
       这是本库一以贯之的底线——**错误必须能被机械地抓住**。

    返回 (是否通过, 无法溯源的数字列表)。
    """
    allowed = {n.replace(",", "") for n in NUM_RE.findall(context)}
    # 年份等无害数字不算（"2026年"这类）
    bad: List[str] = []
    for n in NUM_RE.findall(answer):
        n2 = n.replace(",", "")
        if n2 in allowed:
            continue
        if n2.endswith(".0"):
            n2 = n2[:-2]
        if n2 in allowed:
            continue
        # 允许纯年份
        if re.fullmatch(r"20\d{2}", n2):
            continue
        bad.append(n)
    return (not bad), bad


# 小模型"拒答"的典型措辞
REFUSAL_WORDS = ("没有该数值", "没有相关", "未找到", "无匹配", "无法确定",
                 "无法回答", "没有找到", "不存在该")


def is_refusal(answer: str) -> bool:
    """小模型是否在说"我没有这个数据"。"""
    return any(w in answer for w in REFUSAL_WORDS)


def validate_answer(answer: str, context: str) -> Tuple[bool, List[str]]:
    """
    小模型的完整校验：**既不能编，也不能该答不答**。

    两类失败，都能机械抓住：

    | 失败类型 | 表现 | 判据 |
    |:---|:---|:---|
    | **幻觉**（编数） | 说出了参考数据里没有的数 | `validate_numbers` |
    | **假阴性**（拒答） | 参考数据**明明有**，却说"没有该数值" | 拒答措辞 + 上下文非空 |

    ⚠️ 第二类是实测发现的：Qwen2.5-7B 面对"二厂单位成本"这样
       **上下文里有 6 行数据**的提问，仍然答"参考数据中没有该数值"。
       它没编造，但答案同样无用——而这种失败**光看数字校验是发现不了的**
       （没有数字，自然没有无法溯源的数字）。
       不修的话，用户得到的就是一句莫名其妙的"没有数据"。
    """
    ok, bad = validate_numbers(answer, context)
    if not ok:
        return False, [f"引用了无法溯源的数字：{'、'.join(bad)}"]

    has_rows = bool(context.strip()) and context.strip() != "（无匹配记录）"
    if has_rows and is_refusal(answer):
        return False, ["参考数据非空，却答『没有该数值』（该答没答）"]

    return True, []


# =============================================================================
# 五、执行（含级联升级）
# =============================================================================

def answer(question: str, *, vocab=None, verbose: bool = True,
           workspace_root=None, use_llm: bool = False) -> Dict[str, Any]:
    """Answer only from the selected workspace's current, identified facts.

    Optional Flash can select fact IDs, but cannot alter values, attach them to
    another entity/period, or supply unchecked causal prose. The final rendering
    is deterministic. There is no second-model escalation.
    """
    with _scope(workspace_root):
        ready, why = db_ready()
        if not ready:
            return {"level": "unavailable", "answer": f"无法取数：{why}",
                    "trace": [why], "escalated": False, "model_calls": 0}
        d = route(question, vocab=vocab)
        rows, terms = _lookup(question, vocab, limit=200)
        trace = [d.explain()]
        if not rows or not terms:
            return {"level": "unavailable", "answer": "当前工作区没有与问题范围匹配的已验证指标；请明确对象、指标与期间。",
                    "trace": trace, "escalated": False, "model_calls": 0}
        facts = {f"F{i:04d}": row for i, row in enumerate(rows, 1)}
        selected = list(facts)
        calls = 0
        if use_llm and not d.is_direct:
            # The model only selects existing fact IDs, never writes facts.
            try:
                calls = 1
                reply = _client("large").invoke([
                    ("system", "从给定事实中选择与问题有关的fact_id。只输出JSON对象，格式为{\"fact_ids\":[\"F0001\"]}。不要生成数字、解释、归因或新事实。"),
                    ("user", json.dumps({"facts": facts, "question": question}, ensure_ascii=False))])
                parsed = json.loads((reply.content or "").strip())
                chosen = parsed.get("fact_ids")
                if not isinstance(chosen, list) or not chosen or any(not isinstance(x, str) or x not in facts for x in chosen):
                    raise ValueError("模型返回了无效事实引用")
                selected = list(dict.fromkeys(chosen))
                trace.append("Flash仅选择现有事实编号，数值/主体/期间仍由脚本绑定呈现")
            except Exception as exc:
                trace.append(f"Flash结果未采用（{type(exc).__name__}），展示匹配原始记录")
        output = []
        for fact_id in selected:
            row = facts[fact_id]
            source = row.get("coord") or row.get("article") or row.get("source_article") or "当前数值快照"
            output.append(f"[{fact_id}] {row.get('entity','')} / {row.get('metric','')} / {row.get('period','')} = {row.get('value','')}{row.get('unit') or ''}；来源：{source}。")
        if any(word in question for word in REASON_WORDS):
            output.append("以上为当前资料中的事实；没有同对象、同期间的因果证据时，不能据此确定业务原因。")
        return {"level": L_STATE if d.is_direct else "grounded_facts", "answer": "\n".join(output),
                "trace": trace, "rows": [facts[x] for x in selected], "fact_ids": selected,
                "escalated": False, "model_calls": calls,
                "source_scope": "personal" if _WORKSPACE.get() is not None else "shared"}


# =============================================================================
# 六、自测（不联网，只验路由是确定的）
# =============================================================================

def _selftest() -> int:
    cases = [
        ("银黄口服液 2026-05 的单位成本是多少", "只读单源事实查询"),
        ("银黄口服液和六味地黄胶囊哪个成本高", "跨源对比"),
        ("材料成本为什么上涨", "需要推理"),
        ("帮我生成这个月的报告", "写入/交付物"),
        ("把整改任务派发下去", "RPA"),
        ("导出一份 PDF", "交付物"),
    ]
    print("=" * 74)
    print("路由自测（只看裁决是否落在预期档位）")
    print("=" * 74)
    for q, expect in cases:
        d = route(q)
        print(f"\n提问：{q}")
        print(f"  预期：{expect}")
        print("  " + d.explain().replace("\n", "\n  "))
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="请求路由 + 模型级联")
    ap.add_argument("--ask", help="提问（走完整阶梯）")
    ap.add_argument("--route-only", action="store_true", help="只看路由裁决，不调模型")
    ap.add_argument("--selftest", action="store_true", help="跑路由自测")
    a = ap.parse_args()

    if a.selftest or not a.ask:
        return _selftest()

    if a.route_only:
        print(route(a.ask).explain())
        return 0

    r = answer(a.ask)
    print("=" * 74)
    for line in r["trace"]:
        print(line)
    print("=" * 74)
    print(r["answer"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
