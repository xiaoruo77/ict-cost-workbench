# -*- coding: utf-8 -*-
"""Build a versioned, hash-verified Docker context from code and explicit data.

This copies plaintext files. It does not encrypt, build, or publish an image.
Existing output directories are never deleted or reused. Use --list to review
exact relative paths and --data-root for an external business workspace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout

ensure_utf8_stdout()
HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
CODE_ITEMS = [
    "scripts", "server", "references", "docs", "tests",
    "Dockerfile", "Dockerfile.ict", "Dockerfile.sealed", "docker-compose.yml",
    "requirements.txt", "requirements.lock", "SKILL.md", "INGEST_AGENT.md",
    "AGENT_RUNTIME.md", "README.md", "DEPLOY.md", "系统现状与架构.md",
]
DATA_ITEMS = ["raw", "wiki", "state", "graph", "reports"]
EXCLUDED = {
    "data": "账户与会话运行数据", "exports": "运行时导出",
    ".index": "可再生检索缓存", ".env": "环境凭据", "__pycache__": "编译产物",
    ".git": "版本历史", "node_modules": "安装依赖", "sample_workspace": "独立样例",
}
_SECRET_NAMES = {"credentials.json", "secrets.json", "tokens.json", "auth.json"}
_SECRET_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".pyc"}


def _allowed(relative: Path) -> bool:
    if any(part in EXCLUDED or part.startswith(".") for part in relative.parts):
        return False
    name = relative.name.lower()
    return (name not in _SECRET_NAMES and not name.startswith(".env")
            and relative.suffix.lower() not in _SECRET_SUFFIXES
            and not name.endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm")))


def _plan(code_root: Path, data_root: Path) -> list[tuple[Path, Path]]:
    """Return (source, relative destination) pairs after strict containment checks."""
    code_root, data_root = code_root.resolve(), data_root.resolve()
    for required in ("scripts", "server"):
        if not (code_root / required).is_dir():
            raise ValueError(f"代码目录缺失: {required}")
    for required in ("raw", "wiki"):
        if not (data_root / required).is_dir():
            raise ValueError(f"业务数据目录缺失: {data_root / required}")
    files = {}
    for root, items in ((code_root, CODE_ITEMS), (data_root, DATA_ITEMS)):
        for item in items:
            source = root / item
            if not source.exists():
                continue
            candidates = [source] if source.is_file() else sorted(source.rglob("*"))
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(root)
                if not _allowed(relative):
                    continue
                if not candidate.resolve().is_relative_to(root):
                    raise ValueError(f"打包文件符号链接越出来源根: {relative}")
                if relative in files and files[relative].resolve() != candidate.resolve():
                    raise ValueError(f"打包路径冲突: {relative}")
                files[relative] = candidate
    for required in ("raw", "wiki"):
        if not any(rel.parts[0] == required for rel in files):
            raise ValueError(f"{required}/ 没有可交付文件，拒绝生成空数据包")
    return [(files[relative], relative) for relative in sorted(files)]


def _scan(root: Path, data_root: Path | None = None) -> list[tuple[Path, int]]:
    return [(relative, source.stat().st_size)
            for source, relative in _plan(Path(root), Path(data_root or root))]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assemble(code_root: Path, data_root: Path, out: Path) -> dict:
    code_root, data_root, out = code_root.resolve(), data_root.resolve(), out.resolve()
    if out.exists():
        raise FileExistsError(f"输出已存在，旧版本已保留，请改用新目录: {out}")
    for source_root in {code_root, data_root}:
        if out.is_relative_to(source_root) or source_root.is_relative_to(out):
            raise ValueError("输出目录不能与代码根或数据根重叠")
    files = _plan(code_root, data_root)
    out.mkdir(parents=True, exist_ok=False)
    marker = out / "INCOMPLETE.txt"
    marker.write_text("构建上下文尚未完成；请检查失败原因后使用新的输出目录。", encoding="utf-8")
    records = []
    for source, relative in files:
        before = _sha(source)
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied = _sha(destination)
        if before != copied or before != _sha(source):
            raise RuntimeError(f"打包期间源文件改变或副本不一致: {relative}")
        records.append({"path": relative.as_posix(), "size_bytes": destination.stat().st_size,
                        "sha256": copied, "kind": "data" if relative.parts[0] in DATA_ITEMS else "code"})
    manifest = {"schema_version": 1, "complete": True, "encrypted": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": records, "file_count": len(records)}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                      encoding="utf-8", newline="\n")
    marker.unlink()  # Only our own completion marker; existing outputs are untouched.
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="组装受限数据构建上下文（明文，非加密）")
    parser.add_argument("--out", type=Path, help="必须是代码根和数据根以外的新目录")
    parser.add_argument("--data-root", type=Path,
                        help="外置业务工作区；默认 WIKI_DATA_DIR/LLM_WIKI_WS 或代码根")
    parser.add_argument("--list", action="store_true", help="列出精确复制清单，不写入")
    args = parser.parse_args()
    data_root = args.data_root or Path(os.getenv("WIKI_DATA_DIR") or os.getenv("LLM_WIKI_WS") or WIKI_ROOT)
    out = args.out or (WIKI_ROOT.parent / ("sealed_context_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")))
    try:
        files = _plan(WIKI_ROOT, data_root)
        print(f"业务数据来源: {data_root.resolve()}")
        print("此上下文包含明文业务资料；脚本不执行加密、构建或发布。")
        for source, relative in files:
            print(f"{relative.as_posix()}  {source.stat().st_size} B")
        print(f"文件总数: {len(files)}")
        if args.list:
            return 0
        manifest = assemble(WIKI_ROOT, data_root, out)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"组装未完成: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(f"已生成独立上下文: {out.resolve()}")
    print(f"SHA256逐文件核对通过: {manifest['file_count']} 个；清单 MANIFEST.json")
    print("下一步按部署说明构建并核对镜像内来源哈希；本命令未执行 Docker。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
