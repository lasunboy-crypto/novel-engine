#!/usr/bin/env python3
"""从已提交源码生成不含私人开发材料的公开快照；不上传、不读取本机凭据。"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT_FILES = frozenset({
    ".env.example", ".gitignore", ".python-version", "README.md", "LICENSE",
    "CONTRIBUTING.md", "SECURITY.md", "PUBLIC_RELEASE.md", "PROJECT_SPEC.md",
    "API_CONTRACT.md", "FRONTEND_SPEC.md", "OBSERVABILITY.md", "ROADMAP.md",
    "MIGRATION.md", "alembic.ini", "config.yaml", "dev.sh", "pyproject.toml", "uv.lock",
})
TREE_ROOTS = frozenset({"src", "web", "prompts", "alembic", "config", ".github"})
TEST_ROOTS = frozenset({"api", "cli", "db", "services", "unit", "helpers"})
PUBLIC_SCRIPTS = frozenset({
    "scripts/export_public.py", "scripts/backfill_chapter_summaries.py",
    "scripts/backfill_final_md_for_force_passed.py", "scripts/backfill_card_thresholds.py",
    "scripts/sync_meta_completed_chapters.py",
})
PRIVATE_TEST_MARKERS = (
    b"docs/acceptance", b'"docs"', b"'docs'", b"/Users/", b"git archive", b"git show",
    b"fixtures/", b'"fixtures"', b"'fixtures'", b"scripts/",
)
SECRET_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "cloud_access_id": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bLTAI[A-Za-z0-9]{16,}\b"),
    "provider_token": re.compile(rb"\bsk-(?:ant-[A-Za-z0-9_-]{25,}|[A-Za-z0-9]{25,})\b"),
    "authorization": re.compile(rb"(?i)authorization[\s\"':]+bearer\s+[A-Za-z0-9._-]{20,}"),
    "url_password": re.compile(rb"https?://[^\s/:]+:[^\s/@]{5,}@"),
}
PLACEHOLDER = re.compile(rb"(?i)(?:x{5}|dummy|fake|example|test[-_]|redact|your[-_])")


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def _allowed(name: str, data: bytes, historical_files: set[str]) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if any(part in {".git", ".venv", "__pycache__", "node_modules", "dist", ".codex", ".claude"} for part in path.parts):
        return False
    if path.name.startswith(".env") and name != ".env.example":
        return False
    if path.suffix.lower() in {".pem", ".key", ".db", ".sqlite", ".sqlite3", ".log", ".pyc"}:
        return False
    if name in {"config/local.yaml", "config/local.yml"}:
        return False
    if name in PUBLIC_SCRIPTS:
        return True
    if name in ROOT_FILES or path.parts[0] in TREE_ROOTS:
        return True
    if name.startswith("docs/open-source/"):
        return True
    if name == "docs/local_llm_config.md" or name.startswith("docs/modelfiles/"):
        return True
    if path.parts[0] != "tests":
        return False
    if name == "tests/unit/test_public_export.py":
        return True
    if name == "tests/historical_contract_replay.py" or name in historical_files:
        return False
    if path.name.startswith("test_") and any(marker in data for marker in PRIVATE_TEST_MARKERS):
        return False
    return len(path.parts) == 2 or path.parts[1] in TEST_ROOTS


def export_public(source: Path, output: Path, ref: str = "HEAD") -> dict[str, object]:
    """仅从 Git 对象读取明确范围；任何校验失败均不创建输出目录。"""
    source = source.resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("输出目录必须尚不存在，禁止覆盖已有文件")
    if output.resolve().is_relative_to(source):
        raise ValueError("输出目录必须位于源码目录之外")
    revision = _git(source, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    records = _git(source, "ls-tree", "-r", "-z", revision).split(b"\0")
    history_index = "tests/fixtures/historical_contract_revisions.json"
    entries: list[tuple[str, str, str]] = []
    source_count = 0
    source_paths: set[str] = set()
    for record in records:
        if not record:
            continue
        source_count += 1
        meta, raw_name = record.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode("utf-8")
        source_paths.add(name)
        # 先按路径排除私人材料，不读取被排除的 blob，更不接触本机 ignored 文件。
        if name != history_index and not _allowed(name, b"", set()):
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError(f"公开文件必须是普通文件，拒绝链接或子模块：{name}")
        entries.append((name, mode, oid))
    result = subprocess.run(
        ["git", "-C", str(source), "cat-file", "--batch"],
        input=b"".join(oid.encode() + b"\n" for _, _, oid in entries),
        capture_output=True, check=True,
    ).stdout
    blobs: dict[str, tuple[str, bytes]] = {}
    offset = 0
    for name, mode, _ in entries:
        header_end = result.index(b"\n", offset)
        size = int(result[offset:header_end].split()[2])
        offset = header_end + 1
        blobs[name] = (mode, result[offset:offset + size])
        offset += size + 1
    historical_path = history_index
    history = json.loads(blobs[historical_path][1]) if historical_path in blobs else {}
    historical_files = {node.split("::", 1)[0] for node in history}
    selected: dict[str, tuple[str, bytes]] = {}
    for name, (mode, data) in blobs.items():
        if not _allowed(name, data, historical_files):
            continue
        if mode not in {"100644", "100755"}:
            raise ValueError(f"公开文件必须是普通文件，拒绝链接或子模块：{name}")
        selected[name] = (mode, data)
    public_agent = "docs/open-source/AGENTS.public.md"
    if public_agent not in selected:
        raise ValueError("缺少公开贡献纪律模板")
    # 测试辅助模块可能间接依赖私人样本；按导入关系闭包排除，保持原断言。
    excluded_dependencies: dict[str, list[str]] = {}
    while True:
        rejected: dict[str, list[str]] = {}
        for name, (_, data) in selected.items():
            if not name.startswith("tests/") or not name.endswith(".py"):
                continue
            modules: set[str] = set()
            for node in ast.walk(ast.parse(data, filename=name)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)
                    modules.update(f"{node.module}.{alias.name}" for alias in node.names)
                elif isinstance(node, ast.Import):
                    modules.update(alias.name for alias in node.names)
            missing = []
            for module in modules:
                if not module.startswith(("tests.", "scripts.")):
                    continue
                imported_path = module.replace(".", "/") + ".py"
                if imported_path in source_paths and imported_path not in selected:
                    missing.append(imported_path)
            if missing:
                rejected[name] = sorted(set(missing))
        if not rejected:
            break
        excluded_dependencies.update(rejected)
        for name in rejected:
            selected.pop(name)
    selected_source_count = len(selected)
    selected["AGENTS.md"] = selected[public_agent]
    # 私人历史回放依赖未公开的 Git 提交；相关测试文件已排除，取消插件注册。
    conftest = "tests/conftest.py"
    if conftest in selected:
        mode, data = selected[conftest]
        selected[conftest] = (mode, data.replace(b'pytest_plugins = ["tests.historical_contract_replay"]\n', b""))
    for name, (_, data) in selected.items():
        for label, pattern in SECRET_PATTERNS.items():
            if any(not PLACEHOLDER.search(match.group()) for match in pattern.finditer(data)):
                raise ValueError(f"凭据扫描发现候选，停止导出：{name} ({label})")
    for required in ("README.md", "LICENSE", "pyproject.toml", "config/public_mock.yaml"):
        if required not in selected:
            raise ValueError(f"缺少必需的公开文件：{required}")
    manifest = {
        "schema_version": 1,
        "snapshot_digest": hashlib.sha256(b"".join(
            name.encode() + b"\0" + hashlib.sha256(data).digest()
            for name, (_, data) in sorted(selected.items())
        )).hexdigest(),
        "history_included": False,
        "source_file_count": source_count,
        "published_file_count": len(selected),
        "excluded_source_file_count": source_count - selected_source_count,
        "excluded_dependent_test_files": len(excluded_dependencies),
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                  for name, (_, data) in sorted(selected.items())},
    }
    output.mkdir(parents=True)
    for name, (mode, data) in selected.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode == "100755" else 0o644)
    (output / "PUBLIC_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    try:
        manifest = export_public(args.source, args.output, args.ref)
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"导出失败：{exc}\n")
    print(json.dumps({key: value for key, value in manifest.items() if key != "files"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
