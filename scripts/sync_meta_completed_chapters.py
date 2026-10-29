#!/usr/bin/env python3
"""v1.0.x A2-03(2026-05-19 hot-fix wave 2):一次性扫所有工作区,按
SQLite ``chapters.status='approved'`` 真实计数同步写回 ``meta.json``
``completed_chapters`` 字段。

背景
====

轮 152 A-03 hot-fix 修了 SQLite ORM(``WorkspaceORM.completed_chapters``
首次进 approved 时 += 1 + rewind/force_pass 同步)+ Alembic 0010 backfill
迁移把现有工作区的 ORM 字段从 0 → COUNT(approved)。但 **meta.json 没同步** —
``WorkspaceService.list_workspaces`` 通过 ``read_meta`` 读 meta.json 渲染
前端工作区列表,导致 ORM=N / meta.json=0 永久分裂(luo2 实测 ORM=5 /
meta=0 / 前端永显 0/150)。

A2-03 本任务的代码改动让 chapter_service / chapter_rewind_service 在 ORM
改动 commit 之后调 ``WorkspaceService.sync_completed_chapters_to_meta``
同步 meta.json,**新动作**之后自然对齐;本脚本负责**存量数据**一次性
对齐(扫所有工作区,DB 查 approved count,写回 meta.json)。

用法
====

默认 dry-run(列出会改的工作区,不实际写盘)::

    uv run python scripts/sync_meta_completed_chapters.py

实际写盘::

    uv run python scripts/sync_meta_completed_chapters.py --apply

指定工作区根目录(默认 ``./workspaces/``)::

    uv run python scripts/sync_meta_completed_chapters.py --workspaces-root workspaces/ --apply

仅处理某个工作区::

    uv run python scripts/sync_meta_completed_chapters.py --workspace luo2 --apply

幂等
====

重跑无副作用 — 第二次时所有 meta.json 已与 ORM 对齐,无改动 = 0 写盘。

输出
====

为每个工作区打印 ``meta.completed_chapters`` 与 ``DB COUNT(approved)`` 的
对比,以及是否需要同步::

    workspace=luo2: meta=0 db=5 → 写回 5 [DRY-RUN]
    workspace=luo3: meta=10 db=10 → 已对齐,跳过

不存在 ``data/novel.db`` 的工作区(P1 工作区未做过任何写章动作)→ DB
count=0,通常 meta 也是 0,无改动跳过。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 让本脚本可独立 import 业务模块,无需先 cd 到仓库根
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.workspace.meta import read_meta, write_meta  # noqa: E402
from src.core.workspace.workspace_paths import WorkspacePaths  # noqa: E402


def _count_approved_chapters_in_db(db_path: Path) -> int:
    """SELECT COUNT(*) FROM chapters WHERE status='approved'。

    DB 文件不存在 / chapters 表不存在 → 返 0(P1 工作区未做过任何写章动作
    或 schema 尚未推到 0008+)。
    """
    if not db_path.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM chapters WHERE status = 'approved'"
            )
            return int(cursor.fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _scan_workspace(workspace_id: str, paths: WorkspacePaths) -> tuple[int, int]:
    """返 (meta_count, db_count);任一读失败按 0 兜底(对齐 backfill_card_thresholds
    脚本错误处理风格 — 容忍单工作区损坏不阻塞整批)。"""
    if not paths.meta_json.is_file():
        return (0, 0)
    try:
        workspace = read_meta(paths.meta_json)
        meta_count = workspace.completed_chapters
    except Exception:  # noqa: BLE001
        return (0, 0)
    db_count = _count_approved_chapters_in_db(paths.db_path)
    return (meta_count, db_count)


def _iter_workspace_ids(workspaces_root: Path) -> list[str]:
    """枚举所有工作区(目录 + meta.json 存在),跳过 ``_active`` / 下划线开头
    / 文件 / 软链。"""
    if not workspaces_root.is_dir():
        return []
    ids: list[str] = []
    for entry in sorted(workspaces_root.iterdir()):
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue
        if not (entry / "meta.json").is_file():
            continue
        ids.append(entry.name)
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "v1.0.x A2-03:一次性扫所有工作区,按 SQLite chapters.status='approved' "
            "真实计数同步写回 meta.json completed_chapters。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workspaces-root",
        default="workspaces",
        help="工作区根目录(默认 ./workspaces/)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="仅处理指定工作区 ID(默认处理全部)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写盘(默认 dry-run,只打印不改)",
    )
    args = parser.parse_args(argv)

    workspaces_root = Path(args.workspaces_root).resolve()
    if not workspaces_root.is_dir():
        print(
            f"[ERROR] workspaces 根目录不存在: {workspaces_root}",
            file=sys.stderr,
        )
        return 1

    if args.workspace:
        ids = [args.workspace]
    else:
        ids = _iter_workspace_ids(workspaces_root)

    if not ids:
        print(f"[INFO] {workspaces_root} 下无工作区,无事可做")
        return 0

    dry_run = not args.apply
    suffix = " [DRY-RUN]" if dry_run else ""
    print(
        f"扫描 {len(ids)} 个工作区"
        + (" (dry-run,不写盘)" if dry_run else " (实际写盘)")
    )

    changed = 0
    for ws_id in ids:
        paths = WorkspacePaths(ws_id, workspaces_root=workspaces_root)
        meta_count, db_count = _scan_workspace(ws_id, paths)
        if meta_count == db_count:
            print(f"  workspace={ws_id}: meta={meta_count} db={db_count} → 已对齐,跳过")
            continue
        print(
            f"  workspace={ws_id}: meta={meta_count} db={db_count} → 写回 {db_count}{suffix}"
        )
        if not dry_run:
            try:
                workspace = read_meta(paths.meta_json)
                updated = workspace.model_copy(
                    update={"completed_chapters": db_count}
                )
                write_meta(paths.meta_json, updated)
                changed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    [ERROR] 写回失败: {exc}", file=sys.stderr)

    if dry_run:
        print(f"\n[DRY-RUN] 共 {len(ids)} 个工作区,加 --apply 实际写盘")
    else:
        print(f"\n[OK] 实际改动 {changed} / {len(ids)} 个工作区")
    return 0


if __name__ == "__main__":
    sys.exit(main())
