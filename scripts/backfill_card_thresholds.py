#!/usr/bin/env python3
"""v1.0 hot-fix A-05:既有章节卡 ``review_threshold`` / ``revise_threshold``
按 tension 4 档分档逻辑批量回填。

bug ⑤(轮 150)修了**新生成路径**的阈值,但既有 luo2 ch-0001..0005 / 其他工作区
已落盘 ch-*.json 仍是旧字面默认 ``7.5 / 6.5``,开篇章 tension=2 也是 7.5
→ Qwen reviewer 系统性给不到分死锁。本脚本一次性扫所有工作区按 tension
重算并写回,与新生成路径用同一份 ``_default_thresholds_for_tension`` SSOT。

用法
====

# 默认 dry-run(列出会改的卡,不实际写盘):
uv run python scripts/backfill_card_thresholds.py

# 实际写盘:
uv run python scripts/backfill_card_thresholds.py --apply

# 指定工作区根目录(默认 ./workspaces/):
uv run python scripts/backfill_card_thresholds.py --workspaces-root workspaces/ --apply

# 仅处理某个工作区:
uv run python scripts/backfill_card_thresholds.py --workspace luo2 --apply

# 不保护手动编辑(强行按 4 档覆盖全部既有卡):
uv run python scripts/backfill_card_thresholds.py --apply --force

保护语义(默认)
================

**默认只回填旧字面默认 ``7.5 / 6.5``(bug ⑤ 前一刀切)的卡**;非默认值
(如维护人手动改成 6.0 / 5.5)**不动**,避免覆盖手动调优。

加 ``--force`` 跳过保护逻辑,按 4 档分档表强行重写所有阈值(等价于
"按 tension 重算 + 写回",对齐 v1.0 bug audit §A-05 字面初始描述)。

输出
====

为每个工作区打印 N 张卡的统计 + 改动明细(只列实际有变化的卡):

    workspace=luo2:
      ch-0001.json: tension=2 review 7.5 → 6.0, revise 6.5 → 5.0
      ...
      合计 5 张卡 / 实际改动 5 张

不改卡的工作区:打印 "0 张需要回填,跳过"。

幂等
====

重跑无副作用 —— 第二次时所有阈值已对齐 4 档,无改动 = 0 写盘。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# 让本脚本可独立 import 业务模块,无需先 cd 到仓库根
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.agents.chapter_card_generator import (  # noqa: E402
    _default_thresholds_for_tension,
)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写 JSON(对齐 outline_service._atomic_write_json 同型先例)。"""
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


# bug ⑤ 前的旧字面默认(对齐 src/core/models/chapter_card.py 老 ChapterCard
# 默认 ``review_threshold=7.5 / revise_threshold=6.5``)。保护模式只回填
# 这两个值的卡,避免覆盖维护人手动调优。
_LEGACY_REVIEW_DEFAULT = 7.5
_LEGACY_REVISE_DEFAULT = 6.5


def _process_card(
    card_path: Path, *, apply: bool, force: bool
) -> tuple[str, str | None]:
    """处理单张卡。返回 ``(action, msg)``,action ∈ {"changed", "skipped", "noop"}。

    - ``noop``:阈值已对齐 4 档 / tension 字段缺失 → 不打印
    - ``skipped``:保护模式下非旧默认值 → 打印"保护跳过"提示
    - ``changed``:实际改动 → 打印改动明细
    """
    with open(card_path, encoding="utf-8") as f:
        card = json.load(f)
    tension = card.get("tension_level")
    if not isinstance(tension, int) or not (1 <= tension <= 10):
        return "noop", None
    new_review, new_revise = _default_thresholds_for_tension(tension)
    old_review = card.get("review_threshold")
    old_revise = card.get("revise_threshold")
    if old_review == new_review and old_revise == new_revise:
        return "noop", None
    # 保护模式:跳过非旧默认值的卡(可能是手动编辑)
    if not force and (
        old_review != _LEGACY_REVIEW_DEFAULT
        or old_revise != _LEGACY_REVISE_DEFAULT
    ):
        return "skipped", (
            f"  {card_path.name}: tension={tension} "
            f"review={old_review} revise={old_revise} (非旧默认,保护跳过;"
            f"加 --force 强行覆盖)"
        )
    msg = (
        f"  {card_path.name}: tension={tension} "
        f"review {old_review} → {new_review}, "
        f"revise {old_revise} → {new_revise}"
    )
    if apply:
        card["review_threshold"] = new_review
        card["revise_threshold"] = new_revise
        _atomic_write_json(card_path, card)
    return "changed", msg


def _process_workspace(
    workspace_dir: Path, *, apply: bool, force: bool
) -> tuple[int, int, int, list[str]]:
    """处理一个工作区。返回 ``(total_cards, changed_count, skipped_count, msgs)``。"""
    cards_dir = workspace_dir / "data" / "cards"
    if not cards_dir.is_dir():
        return 0, 0, 0, []
    total = 0
    changed = 0
    skipped = 0
    msgs: list[str] = []
    for card_path in sorted(cards_dir.glob("ch-*.json")):
        total += 1
        action, msg = _process_card(card_path, apply=apply, force=force)
        if action == "changed":
            changed += 1
        elif action == "skipped":
            skipped += 1
        if msg is not None:
            msgs.append(msg)
    return total, changed, skipped, msgs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按 tension 4 档分档批量回填已有 ch-*.json 卡阈值。"
    )
    parser.add_argument(
        "--workspaces-root",
        type=Path,
        default=_REPO_ROOT / "workspaces",
        help="工作区根目录(默认 ./workspaces)",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="仅处理指定工作区 ID(默认全部)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写盘;不加则 dry-run(列出会改的卡)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "跳过保护模式,按 4 档分档表强行重写所有阈值。"
            "默认开启保护:仅回填旧字面默认 7.5/6.5 的卡(避免覆盖手动编辑)"
        ),
    )
    args = parser.parse_args(argv)

    root: Path = args.workspaces_root.resolve()
    if not root.is_dir():
        print(f"[错] workspaces 根目录不存在: {root}", file=sys.stderr)
        return 2

    if args.workspace is not None:
        workspaces = [root / args.workspace]
        if not workspaces[0].is_dir():
            print(f"[错] 工作区不存在: {workspaces[0]}", file=sys.stderr)
            return 2
    else:
        # 跳过下划线开头的隐藏目录(_active 软链 / _system_metrics.db 等)
        workspaces = sorted(
            p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")
        )

    if not args.apply:
        print("[dry-run] 不实际写盘 — 加 --apply 启动真写回")
    if args.force:
        print("[force] 跳过保护,按 4 档强行覆盖全部卡")
    else:
        print("[保护] 仅回填旧默认 7.5/6.5 的卡;非默认值保护跳过 (加 --force 强行覆盖)")
    print(f"扫工作区根: {root}")
    grand_total = 0
    grand_changed = 0
    grand_skipped = 0
    for ws_dir in workspaces:
        total, changed, skipped, msgs = _process_workspace(
            ws_dir, apply=args.apply, force=args.force
        )
        grand_total += total
        grand_changed += changed
        grand_skipped += skipped
        if total == 0:
            continue
        print(f"workspace={ws_dir.name}:")
        for m in msgs:
            print(m)
        verb = "实际改动" if args.apply else "需要回填"
        print(
            f"  合计 {total} 张卡 / {verb} {changed} 张"
            + (f" / 保护跳过 {skipped} 张" if skipped else "")
        )
    print()
    print(
        f"全部:{grand_total} 张卡 / "
        f"{'改动' if args.apply else '需要回填'} {grand_changed} 张"
        + (f" / 保护跳过 {grand_skipped} 张" if grand_skipped else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
