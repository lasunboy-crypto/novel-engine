#!/usr/bin/env python3
"""v1.0.x reviewer-resilience(2026-05-20):一次性扫所有工作区,补齐
``status=approved`` 但缺 ``final.md`` 的章节文件(从 ``v{current_version}.md``
复制)。

背景
====

P7-B-06 后真 Qwen reviewer 偶发 JSON parse fail(本地 LLM 自由输出格式
不稳),两次失败 + 老 ``_REVIEWER_MAX_ATTEMPTS=2`` 必升人工(luo2 ch-0001
实测命中)。用户唯一恢复路径是 ``force_pass``,但 ``ChapterRewindService.force_pass``
只改 ORM status='approved' / completed_chapters += 1,**没调 ``versioning.mark_final``
把 ``v{current_version}.md`` 复制成 ``final.md``** → ExportService 只读
``final.md`` → 导出文件正文全是"(章节未生成)"占位。

v1.0.x reviewer-resilience 修法:
- ``force_pass`` 补 ``mark_final`` 调用(新动作自然对齐)
- ``ExportService._collect_chapter_payloads`` fallback 读 ``v{current_version}.md``
  (运行时兜底)
- **本脚本负责存量数据**:扫所有工作区,把"status=approved 但缺 final.md"
  的章节从 v{current_version}.md 复制一份成 final.md(对齐 force_pass 新行为)

用法
====

默认 dry-run(列出会改的章节,不实际写盘)::

    uv run python scripts/backfill_final_md_for_force_passed.py

实际写盘::

    uv run python scripts/backfill_final_md_for_force_passed.py --apply

指定工作区根目录(默认 ``./workspaces/``)::

    uv run python scripts/backfill_final_md_for_force_passed.py --workspaces-root workspaces/ --apply

仅处理某个工作区::

    uv run python scripts/backfill_final_md_for_force_passed.py --workspace luo2 --apply

幂等
====

重跑无副作用 — 第二次时所有 approved 章节 final.md 都已对齐,无改动 = 0 写盘。

输出
====

为每个工作区打印章节级状态::

    workspace=luo2:
      ch-0001: approved, current_version=1, final.md 缺失 → 复制 v1.md [DRY-RUN]
      ch-0002: drafting,跳过(非 approved)
      ch-0003: approved, final.md 已存在,跳过
    workspace=luo3: 无 approved 章节,跳过

错误处理
========

- 工作区无 ``data/novel.db`` 或 chapters 表 → 跳过(P1 工作区或 schema
  未推到 0008+)
- ``v{current_version}.md`` 文件不存在 → 打印 ``[WARN]`` 跳过,不阻塞整批
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.workspace.workspace_paths import WorkspacePaths  # noqa: E402


def _load_approved_chapters(
    db_path: Path,
) -> list[tuple[str, int]]:
    """SELECT chapter_id, current_version FROM chapters WHERE status='approved'。

    DB / 表不存在 → 返空列表(对齐 sync_meta_completed_chapters 同模式)。
    """
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            cursor = conn.execute(
                "SELECT chapter_id, current_version FROM chapters "
                "WHERE status = 'approved' ORDER BY chapter_id"
            )
            return [(row[0], int(row[1])) for row in cursor.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _iter_workspace_ids(workspaces_root: Path) -> list[str]:
    """枚举所有合法工作区(同 sync_meta_completed_chapters 规则)。"""
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


def _process_workspace(
    workspace_id: str,
    paths: WorkspacePaths,
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    """返 (copied, skipped_already_final, skipped_missing_source)。"""
    approved = _load_approved_chapters(paths.db_path)
    if not approved:
        print(f"workspace={workspace_id}: 无 approved 章节,跳过")
        return (0, 0, 0)

    suffix = " [DRY-RUN]" if dry_run else ""
    print(f"workspace={workspace_id}:")
    copied = 0
    skipped_final = 0
    skipped_missing = 0

    for chapter_id, current_version in approved:
        final_path = paths.chapter_version_file(chapter_id, "final.md")
        if final_path.is_file():
            print(f"  {chapter_id}: final.md 已存在,跳过")
            skipped_final += 1
            continue

        if current_version <= 0:
            print(
                f"  {chapter_id}: [WARN] current_version={current_version} 异常,跳过"
            )
            skipped_missing += 1
            continue

        source_path = paths.chapter_version_file(
            chapter_id, f"v{current_version}.md"
        )
        if not source_path.is_file():
            print(
                f"  {chapter_id}: [WARN] 源版本文件 v{current_version}.md 不存在,跳过"
            )
            skipped_missing += 1
            continue

        print(
            f"  {chapter_id}: approved, current_version={current_version}, "
            f"final.md 缺失 → 复制 v{current_version}.md{suffix}"
        )
        if not dry_run:
            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, final_path)
                copied += 1
            except OSError as exc:
                print(
                    f"  {chapter_id}: [ERROR] 复制失败: {exc}",
                    file=sys.stderr,
                )
                skipped_missing += 1
        else:
            copied += 1

    return (copied, skipped_final, skipped_missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "v1.0.x reviewer-resilience:一次性扫所有工作区,把 status=approved "
            "但缺 final.md 的章节从 v{current_version}.md 复制成 final.md。"
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
    print(
        f"扫描 {len(ids)} 个工作区"
        + (" (dry-run,不写盘)" if dry_run else " (实际写盘)")
    )
    print(f"workspaces_root={workspaces_root}")
    print()

    total_copied = 0
    total_skipped_final = 0
    total_skipped_missing = 0

    for ws_id in ids:
        paths = WorkspacePaths(ws_id, workspaces_root=workspaces_root)
        copied, skipped_final, skipped_missing = _process_workspace(
            ws_id, paths, dry_run=dry_run
        )
        total_copied += copied
        total_skipped_final += skipped_final
        total_skipped_missing += skipped_missing

    print()
    print(f"汇总:工作区 {len(ids)} 个")
    print(f"  复制 final.md:{total_copied}{'(预期,dry-run)' if dry_run else ''}")
    print(f"  已对齐跳过:{total_skipped_final}")
    print(f"  源缺失跳过:{total_skipped_missing}")
    if dry_run and total_copied > 0:
        print()
        print("加 --apply 实际写盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
