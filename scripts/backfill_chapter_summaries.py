#!/usr/bin/env python3
"""v1.0.x wave 4 session 3a:一次性扫工作区,对历史 approved 章节调 LLM 抽取
章节摘要,落 ``chapter_summaries`` 表。

背景
====

wave 4 session 3a 加了 ``ChapterSummaryORM`` + ``extract_chapter_summary``
LLM agent + 章节完成 hook(``ChapterService`` + ``ChapterRewindService``)。**新
动作**之后 approve 的章节自然落摘要;但**历史已 approved 的章节**(luo1 /
luo2 / p3e2e-demo 等 wave 4 之前的工作区)需要一次性 backfill。

本脚本扫指定工作区 → SELECT chapter_id FROM chapters WHERE status='approved'
→ 对每章读 final.md + ChapterCard → 调 LLM 抽取 → 落 ORM。**幂等**:重跑时
chapter_summaries 表已存在的行跳过,只补缺失。

用法
====

默认 dry-run(列出会处理的章节,不实际调 LLM / 不写盘)::

    uv run python scripts/backfill_chapter_summaries.py

实际 backfill(需配 LLM_PROVIDER 选 mock / ollama / claude)::

    LLM_PROVIDER=ollama uv run python scripts/backfill_chapter_summaries.py --apply

仅处理某个工作区::

    LLM_PROVIDER=ollama uv run python scripts/backfill_chapter_summaries.py \\
        --workspace luo2 --apply

涉及 luo1 时强制 stdin 确认(Q3=A 安全保护,物理快照 luo1 不可误改)::

    # 全扫且 --apply 含 luo1 → 提示二次确认
    LLM_PROVIDER=ollama uv run python scripts/backfill_chapter_summaries.py --apply

幂等
====

重跑无副作用 — 已有 ``chapter_summaries`` 行的章节自动跳过。中断后再跑会从上
次未处理的章节继续。

输出
====

为每个工作区打印 "[K/N] workspace=X chapter=ch-XXXX → ✅ persisted" 或对应跳
过/失败原因。最后总结全工作区 ``成功 / 跳过 / 失败`` 计数。

LLM 失败时(404 / 超时 / 解析失败)单章 swallow + warn,不阻塞批 — 对齐
``extract_and_persist_chapter_summary_safely`` 同 fail-open 模式。

环境变量
========

- ``LLM_PROVIDER``:``ollama`` / ``claude`` / ``mock``(默认 ``mock``,对齐
  CLAUDE.md §维护人环境约束)。手动跑时显式设 ``ollama`` 走真 Qwen。
- ``NOVEL_DB_PATH``:不需要(本脚本按工作区自己的 ``data/novel.db``)。
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

from src.config import settings  # noqa: E402
from src.core.workspace.workspace_paths import WorkspacePaths  # noqa: E402

# luo1 物理快照保护通配:工作区名 == "luo1" 或前缀 "_archive_luo1_" 触发警告
_LUO1_PROTECTED_NAMES: tuple[str, ...] = ("luo1",)
_LUO1_ARCHIVE_PREFIX = "_archive_luo1_"


def _is_protected_workspace(workspace_id: str) -> bool:
    return (
        workspace_id in _LUO1_PROTECTED_NAMES
        or workspace_id.startswith(_LUO1_ARCHIVE_PREFIX)
    )


def _list_approved_chapter_ids(db_path: Path) -> list[str]:
    """SELECT chapter_id FROM chapters WHERE status='approved' ORDER BY chapter_id。

    DB 文件不存在 / chapters 表不存在 / SQL 错误 → 返空 list(P1 工作区 / 工作
    区未做过任何写章动作)。
    """
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            cursor = conn.execute(
                "SELECT chapter_id FROM chapters "
                "WHERE status = 'approved' ORDER BY chapter_id"
            )
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _already_has_summary(db_path: Path, chapter_id: str) -> bool:
    """检查 chapter_summaries 表是否已有该 chapter_id 行(幂等防重抽)。"""
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            cursor = conn.execute(
                "SELECT 1 FROM chapter_summaries WHERE chapter_id = ? LIMIT 1",
                (chapter_id,),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        # chapter_summaries 表不存在(Alembic 未升到 0012)→ 视作未抽过(后续
        # 落 ORM 时会因表缺失自然抛,extract_and_persist 内 swallow)
        return False


def _iter_workspace_ids(workspaces_root: Path) -> list[str]:
    """枚举所有工作区(目录 + meta.json 存在),跳过 ``_active`` / 文件 / 软链。

    **不**跳过 ``_archive_luo1_*`` — 由 ``_is_protected_workspace`` 在
    --apply 时单独警告。
    """
    if not workspaces_root.is_dir():
        return []
    ids: list[str] = []
    for entry in sorted(workspaces_root.iterdir()):
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        # 跳过 _active 软链占位 / 不含 meta 的目录
        if entry.name == "_active":
            continue
        if not (entry / "meta.json").is_file():
            continue
        ids.append(entry.name)
    return ids


def _process_workspace(
    workspace_id: str,
    workspaces_root: Path,
    *,
    dry_run: bool,
    llm_client: object,
    llm_model: str,
    session_factory: object,
) -> tuple[int, int, int]:
    """对单个工作区跑 backfill。返 (persisted, skipped, failed) 计数。"""
    paths = WorkspacePaths(workspace_id, workspaces_root=workspaces_root)
    db_path = paths.db_path
    chapter_ids = _list_approved_chapter_ids(db_path)
    if not chapter_ids:
        print(f"  workspace={workspace_id}: 无 approved 章节,跳过")
        return (0, 0, 0)

    print(
        f"  workspace={workspace_id}: 共 {len(chapter_ids)} 个 approved 章节"
    )

    persisted = 0
    skipped = 0
    failed = 0
    for idx, chapter_id in enumerate(chapter_ids, start=1):
        prefix = f"    [{idx}/{len(chapter_ids)}] chapter={chapter_id}"
        if _already_has_summary(db_path, chapter_id):
            print(f"{prefix} → ⏭️  已有摘要,跳过(幂等)")
            skipped += 1
            continue

        if dry_run:
            print(f"{prefix} → 🔍 [DRY-RUN] 将调 LLM 抽取(实际跑加 --apply)")
            continue

        # 实际抽取 + 落 ORM(局部 import 避顶层副作用)
        from src.core.agents.chapter_summary_extractor import (
            extract_and_persist_chapter_summary_safely,
        )

        try:
            extract_and_persist_chapter_summary_safely(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                trigger="reviewer_pass",  # backfill 按 reviewer_pass 默认
                paths=paths,
                session_factory=session_factory,
                llm_client=llm_client,  # type: ignore[arg-type]
                llm_model=llm_model,
            )
            # 抽取成功不一定意味着 ORM 落行成功(LLM fail-open 可能返 None)
            # 重新查 chapter_summaries 表确认
            if _already_has_summary(db_path, chapter_id):
                print(f"{prefix} → ✅ persisted")
                persisted += 1
            else:
                print(f"{prefix} → ⚠️  LLM fail-open(无 ORM 行,详见 structlog)")
                failed += 1
        except Exception as exc:  # noqa: BLE001 — 单章失败不阻塞批
            print(f"{prefix} → ❌ {type(exc).__name__}: {exc}")
            failed += 1

    return (persisted, skipped, failed)


def _confirm_luo1_apply(workspace_ids: list[str]) -> bool:
    """涉及 luo1 / _archive_luo1_* 时 stdin 输入 'y' 确认(Q3=A 安全策略)。

    返 True = 继续;False = 用户拒绝。
    """
    protected = [ws for ws in workspace_ids if _is_protected_workspace(ws)]
    if not protected:
        return True
    print(
        f"\n⚠️  即将对受保护工作区 --apply:{', '.join(protected)}"
        f"\n   luo1 是 wave 4 物理快照基线,backfill 会改 SQLite + 触发 LLM 调用。"
        f"\n   确认继续?输入 'y' 回车,其他取消。"
    )
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        # 非交互环境(CI / 管道)默认拒绝,要求显式确认
        print("非交互环境 — 默认拒绝处理受保护工作区。")
        return False
    if answer == "y":
        print("✅ 用户确认,继续。")
        return True
    print("❌ 用户取消。")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "v1.0.x wave 4 session 3a:一次性扫工作区对历史 approved 章节调"
            " LLM 抽取章节摘要,落 chapter_summaries 表。幂等。"
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
        help="实际跑 LLM 抽取 + 写盘(默认 dry-run)",
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
        workspace_ids = [args.workspace]
    else:
        workspace_ids = _iter_workspace_ids(workspaces_root)

    if not workspace_ids:
        print(f"[INFO] {workspaces_root} 下无工作区,无事可做")
        return 0

    dry_run = not args.apply
    print(
        f"扫描 {len(workspace_ids)} 个工作区"
        + (" (dry-run,不调 LLM 不写盘)" if dry_run else " (实际跑 LLM + 写盘)")
    )
    print(f"LLM_PROVIDER={settings.llm_provider}")

    # Q3=A 安全策略:--apply 涉及 luo1 时 stdin 确认
    if not dry_run and not _confirm_luo1_apply(workspace_ids):
        return 2

    # 构造 LLM client + session_factory(--apply 时才需要)
    llm_client: object = None
    llm_model: str = ""
    session_factory: object = None
    if not dry_run:
        from src.api.deps import (  # noqa: PLC0415 — 延迟 import 避免 dry-run 时副作用
            _make_session_factory,
            _resolve_bootstrap_target,
            make_llm_client,
        )

        # v1.0.x wave 6 s2.5 hotfix(2026-05-25):LLM_PROVIDER=mock 时显式
        # 走 MockLLMClient,绕过 _resolve_bootstrap_target 派发(对齐测试
        # ``tests/scripts/test_backfill_chapter_summaries.py:178 _run_script``
        # 字面意图"--apply + LLM_PROVIDER=mock → MockLLMClient" — 轮 173
        # routing completion fixup 改 bootstrap 派发后接力遗漏,主仓 default
        # = local_qwen_32k 时 OllamaClient 构造无 key 不挂掩盖了这个 bug;
        # s2.5 切云端 default 后 OpenAICompatibleClient 缺 key 抛错暴露)。
        if settings.llm_provider == "mock":
            from src.core.llm.mock_client import MockLLMClient  # noqa: PLC0415

            llm_model = "mock-backfill"
            llm_client = MockLLMClient()
        else:
            # post-wave5 H1 routing completion fixup(2026-05-24)起,bootstrap LLM
            # 客户端按 engine config.yaml default profile 三元组派发,不再依赖
            # settings.llm_provider 全局开关(mock 例外见上)。
            llm_provider, llm_model, llm_base_url = _resolve_bootstrap_target()
            llm_client = make_llm_client(
                llm_model, base_url=llm_base_url, provider=llm_provider
            )
        session_factory = _make_session_factory(workspaces_root)

    total_persisted = 0
    total_skipped = 0
    total_failed = 0
    for ws_id in workspace_ids:
        p, s, f = _process_workspace(
            ws_id,
            workspaces_root,
            dry_run=dry_run,
            llm_client=llm_client,
            llm_model=llm_model,
            session_factory=session_factory,
        )
        total_persisted += p
        total_skipped += s
        total_failed += f

    print()
    if dry_run:
        print(
            f"[DRY-RUN] 共扫 {len(workspace_ids)} 工作区,加 --apply 实际跑"
        )
    else:
        print(
            f"[OK] 完成 — persisted={total_persisted} / "
            f"skipped={total_skipped} / failed={total_failed}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
