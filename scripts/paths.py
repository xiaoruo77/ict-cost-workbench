# -*- coding: utf-8 -*-
"""
paths.py — **路径解析**（让代码既能跑在本机，也能跑在容器里）

为什么需要这一层
----------------
SQLite 库路径原先硬编码为 `F:\\llm-wiki.db`（六处），这在开发机上没问题，
但**部署到容器就崩**——容器里没有 F 盘。

现在按**数据根目录**（data root）解析，三级回退：

    ① 环境变量 `LLM_WIKI_DATA`   ← 指向整个数据根（最高优先）
    ② `/data`                    ← 容器约定挂载点（存在且可写时用）
    ③ 仓库内的 `data/`           ← 本机便携默认

库都挂在数据根下，**按职责分三个**（见下）：

    <data_root>/auth.db                 认证目录：谁是谁 + 令牌
    <data_root>/shared/knowledge.db     知识库镜像：全体共享、只读、**可重建**
    <data_root>/users/<id>.db           个人对话/摘要/偏好：**不可重建**

⚠️ **注意 `LLM_WIKI_DB` 是历史遗留**（本模块下面仍定义 `ENV_KEY`，
   但只在 `describe()` 里读来做提示文字，**不参与解析**）。
   早先版本只有一个库文件，用 `LLM_WIKI_DB` 指它；改成"数据根 + 三库"之后，
   这个变量就没有解析作用了。
   若在部署脚本里看到它，**改它不会有任何效果**——要改的是 `LLM_WIKI_DATA`。
   （陈旧配置是最难查的一类坑：设了、看着生效、其实被忽略。）

用法
----
    from paths import default_db, knowledge_db, auth_db, user_db
    DB = default_db()          # 现在等价于 knowledge_db()

命令行覆盖（各脚本的 `--db`）优先级最高，直接传入即可。
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

# 容器/部署里的约定挂载点。用 `/data` 是因为它是**卷**挂载的常见位置，
# 且与代码目录分开——记忆必须独立于镜像存活（镜像重建不该丢对话）。
CONTAINER_DB = Path("/data/llm-wiki.db")

ENV_KEY = "LLM_WIKI_DB"

# 本机既有数据位置（仅当该盘存在才使用）
LEGACY_DB = Path(r"F:\llm-wiki.db")


def default_db() -> Path:
    """
    解析默认库路径。**不创建文件**，只选路径。

    ⚠️ 这是**旧接口**，返回的是"单一数据库"时代的路径。
    新版按职责分了三个库（见下），但 `default_db()` 仍被若干脚本使用，
    故保留为"知识库镜像"的路径——它是共享的、可重建的那一份。
    """
    return knowledge_db()


# =============================================================================
# 三个库：认证目录 / 共享知识库 / 每用户库
#
# 为什么要分开（而不是一个库里按 user_id 分）
# -------------------------------------------
# 原先"当前用户"由客户端自报的 `user` 参数决定——改成任何人的名字就能读到
# 那个人的全部对话。但**只加认证还不够**：如果所有用户的数据仍在一个文件里，
# 一次 SQL 写错、一个忘了带 user_id 的查询，就会跨用户泄漏。
#
# 物理分开后，"读到别人的数据"这件事在**文件层面**就不可能发生：
# 服务 alice 时打开的压根不是 bob 的库。
#
#     data/
#     ├── auth.db           ← 认证目录：谁是谁 + 令牌。**唯一必须先知道的**
#     ├── shared/
#     │   └── knowledge.db  ← 知识库镜像（wiki/raw/graph）。全体共享、只读、可重建
#     └── users/
#         ├── alice.db      ← alice 的对话/摘要/偏好
#         └── bob.db
#
# ⭐ **知识库绝不能按用户复制**：它是在全公司共享的成本事实源，
#    复制多份必然各自漂移（A 看到旧结论、B 看到新的），
#    那就违背了本库"唯一事实源"的根基。
# =============================================================================

DATA_ROOT_ENV = "LLM_WIKI_DATA"
CONTAINER_DATA_ROOT = Path("/data")


def data_root() -> Path:
    """数据根目录。三级回退：环境变量 > 容器 /data > 仓库内 data/。"""
    env = os.getenv(DATA_ROOT_ENV)
    if env:
        return Path(env)
    if CONTAINER_DATA_ROOT.exists() and os.access(CONTAINER_DATA_ROOT, os.W_OK):
        return CONTAINER_DATA_ROOT
    # 本机既有单库（F:）→ 数据根取它所在的位置，便于迁移时同盘
    return WIKI_ROOT / "data"


def auth_db() -> Path:
    """认证目录库（users + api_tokens）。"""
    return data_root() / "auth.db"


def knowledge_db() -> Path:
    """共享知识库镜像（mirror_*）。全体用户读同一个。"""
    return data_root() / "shared" / "knowledge.db"


def user_db_key(external_id: str) -> str:
    """
    外部用户标识 → 安全的文件名。

    ⚠️ 必须防目录穿越：`../../etc/passwd` 这类标识直接当文件名会写到仓库外。
    只保留"字母数字与 ._@-" 三类之外一律替换为 `_`，并限制长度。
    """
    import re as _re
    import hashlib
    if not isinstance(external_id, str) or not external_id.strip():
        raise ValueError("工作区身份必须是非空 external_id 字符串，不能传用户对象")
    value = external_id.strip()
    k = _re.sub(r"[^0-9A-Za-z._@一-鿿-]", "_", value).strip("._")
    # Existing normal account names retain their directory. For nonportable IDs
    # preserve identity with a digest instead of collapsing two users to one path.
    if k != value or len(k) > 64:
        return (k[:43] or "user") + "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return k


def user_db(external_id: str) -> Path:
    """某个用户的私有库。**只有这份含他的对话历史。**"""
    return data_root() / "users" / f"{user_db_key(external_id)}.db"


# =============================================================================
# 两个根 —— 必须分清，否则是越权漏洞
#
#   SPEC_ROOT  代码仓库。放 SKILL.md / INGEST_AGENT.md / references/ / scripts/。
#              **永远不接受用户输入。**
#   WS_ROOT    个人工作区。放该用户上传的 raw/ 与编译出的 wiki/。
#
# ⚠️ **为什么必须分开**：如果把两者混成一个根，用户只要上传一个 `SKILL.md`
#    就能改掉整个摄入规则——包括"不许编造数字""每个数值必须带坐标"
#    这些根本约束。那等于让被审的人自己写审计标准。
# =============================================================================

SPEC_ROOT = WIKI_ROOT          # 代码仓库根，常量，永不改变


def user_workspace(external_id: str) -> Path:
    """
    某个用户的个人工作区。

        data/users/<id>/ws/
        ├── inbox/     上传暂存（投递区）
        ├── raw/       个人原始资料（含各自的 .hashes.json 去重索引）
        ├── wiki/      个人编译出的词条（对共享基线的 overlay）
        ├── state/     个人关键数值快照
        └── .index/    个人层向量索引

    **共享基线不受这里任何操作影响**——这保住了"唯一事实源"。
    """
    return data_root() / "users" / user_db_key(external_id) / "ws"


def ensure_workspace(external_id: str) -> Path:
    """
    建好工作区骨架并返回根。幂等。

    `.index/` 与 `graph/` 也一并建：检索索引与图谱是**按用户各自一份**的
    （合并了共享基线与该用户的增量），必须落在他自己的工作区里。
    """
    ws = user_workspace(external_id)
    for sub in ("inbox", "raw", "wiki", "state", ".index", "graph", "reports", "exports", "costing_datasets"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


def recover_legacy_costing_datasets(external_id: str, user_id: int | str) -> list[str]:
    """Copy verified owned CSVs from the old dict-derived workspace, preserving it.

    Only the exact historical directory family is considered. Ownership comes
    from the persisted manifest, never a browser parameter or display name.
    Copies are staged and renamed; no old file is removed or overwritten.
    """
    import hashlib
    import json
    import re
    import secrets
    import shutil
    ws = ensure_workspace(external_id).resolve()
    users = (data_root() / "users").resolve()
    recovered = []
    legacy_prefix = re.sub(r"[^0-9A-Za-z._@一-鿿-]", "_",
                           str({"user_id": int(user_id), "external_id": external_id})).strip("._")
    for old in users.glob("user_id_*external_id_*"):
        if not (old.name == legacy_prefix or old.name.startswith(legacy_prefix + "_")):
            continue
        if old.is_symlink() or not old.is_dir() or old == ws.parent:
            continue
        for manifest_path in (old / "ws" / "costing_datasets").glob("csv-*/manifest.json"):
            source = manifest_path.parent
            if source.is_symlink() or manifest_path.is_symlink() or not re.fullmatch(r"csv-[0-9a-f]{16}", source.name):
                continue
            if not source.resolve().is_relative_to(users):
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (str(manifest.get("uploaded_by")) not in {str(user_id), external_id}
                        or manifest.get("dataset_id") != source.name
                        or not isinstance(manifest.get("schema"), dict)
                        or manifest["schema"].get("plugin_id", "csv-generic") != "csv-generic"):
                    continue
                raw_path = source / "data.csv"
                if raw_path.is_symlink():
                    continue
                raw = raw_path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != manifest.get("data_sha256"):
                    continue
                dest = ws / "costing_datasets" / source.name
                if dest.exists():
                    continue
                stage = dest.with_name(".recover-" + secrets.token_hex(8))
                try:
                    stage.mkdir()
                    (stage / "data.csv").write_bytes(raw)
                    (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                    stage.rename(dest)
                    recovered.append(source.name)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            except (OSError, ValueError, TypeError):
                continue
    return recovered


def ensure_parent(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def describe(db: Path) -> str:
    """
    给日志/健康检查用的一句话说明这个路径是从哪来的。

    ⚠️ 判据从 `LLM_WIKI_DB` 改成 `LLM_WIKI_DATA`。
       前者不再参与解析（见模块 docstring），若继续拿它当判据，
       就会出现"设了 LLM_WIKI_DB 但路径其实来自别处"——
       日志言之凿凿地指错方向，比不打印还坏。
    """
    s = str(db)
    root = data_root()
    if os.getenv(DATA_ROOT_ENV):
        return f"{s}  （数据根来自环境变量 {DATA_ROOT_ENV}）"
    if root == Path("/data"):
        return f"{s}  （容器卷 /data）"
    return f"{s}  （仓库内 data/ 默认）"


def describe_all() -> str:
    """三库总览，供 /api/health 与启动日志使用。"""
    r = data_root()
    return (f"数据根 {r}\n"
            f"  认证目录   {auth_db()}\n"
            f"  共享知识库 {knowledge_db()}\n"
            f"  用户库目录 {r / 'users'}")


if __name__ == "__main__":
    db = default_db()
    print(f"默认库路径: {describe(db)}")
    print(f"  存在: {db.exists()}   父目录可写: {os.access(db.parent, os.W_OK)}")
    print(f"  尺寸: {db.stat().st_size if db.exists() else 0} B")
