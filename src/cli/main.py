"""CLI 入口与 argparse 树构造(P1-B-21)。

``pyproject.toml [project.scripts]`` 已挂 ``novel = "src.cli.main:main"``,
``uv run novel <subcommand>`` 走本入口。

CLI 库选择 = argparse(零依赖,贴合 CLAUDE.md §硬约束 1 "不引入文档外依赖")。
顶层命令 11 个:9 个工作区子命令 + 3 个章节命令(write / rewrite / revise)。

启动副作用:
- ``configure_logging()``:与 API 共用 ``src.api.logging`` 配置(实际是通用
  structlog 配置;P1-B-17 落地时已抽离为 API/CLI 共用)。
- argparse 解析 + dispatch 分发;dispatch 统一捕获业务异常翻退出码。

每个子命令 handler 见 ``src/cli/commands.py``,本模块只负责"argparse 树 +
启动 wiring",不写任何业务逻辑。
"""

from __future__ import annotations

import argparse
import sys

from src.api.logging import configure_logging
from src.cli import commands


def build_parser() -> argparse.ArgumentParser:
    """构造完整 argparse 树。导出供测试用 ``parser.parse_args(...)`` 直接验证。"""

    parser = argparse.ArgumentParser(
        prog="novel",
        description="长篇小说创作引擎 CLI(P1)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ---- workspace 子命令组 ----------------------------------------------------
    ws = subparsers.add_parser("workspace", help="工作区生命周期管理")
    ws_sub = ws.add_subparsers(dest="ws_command", required=True)

    ws_create = ws_sub.add_parser("create", help="创建工作区(默认自动激活)")
    ws_create.add_argument("workspace_id", help="工作区 ID(目录名)")
    ws_create.add_argument(
        "--display-name", required=True, help="人类可读名称"
    )
    ws_create.add_argument("--genre", required=True, help="题材,如 scifi")
    ws_create.add_argument("--premise", required=True, help="一句话设定")
    ws_create.add_argument(
        "--total-chapters", type=int, default=0, help="计划总章数,默认 0"
    )
    ws_create.add_argument(
        "--no-activate",
        action="store_true",
        help="创建后不自动切到 _active 软链",
    )
    ws_create.set_defaults(handler=commands.cmd_workspace_create)

    ws_list = ws_sub.add_parser("list", help="列出所有工作区(* 标当前激活)")
    ws_list.set_defaults(handler=commands.cmd_workspace_list)

    ws_switch = ws_sub.add_parser("switch", help="切换激活工作区")
    ws_switch.add_argument("workspace_id")
    ws_switch.set_defaults(handler=commands.cmd_workspace_switch)

    ws_info = ws_sub.add_parser("info", help="查看工作区元数据")
    ws_info.add_argument(
        "workspace_id",
        nargs="?",
        default=None,
        help="工作区 ID;缺省读 _active 软链",
    )
    ws_info.set_defaults(handler=commands.cmd_workspace_info)

    ws_clean = ws_sub.add_parser(
        "clean",
        help="清理工作区运行时数据(chroma / archives / logs;用户数据保留)",
    )
    ws_clean.add_argument("workspace_id", nargs="?", default=None)
    ws_clean.set_defaults(handler=commands.cmd_workspace_clean)

    ws_archive = ws_sub.add_parser(
        "archive",
        help="归档工作区到项目级 archives/<id>.tar.gz(原目录删除)",
    )
    ws_archive.add_argument("workspace_id", nargs="?", default=None)
    ws_archive.set_defaults(handler=commands.cmd_workspace_archive)

    ws_purge = ws_sub.add_parser(
        "purge",
        help="彻底删除工作区目录(不可逆;不动顶级 archives 备份)",
    )
    ws_purge.add_argument("workspace_id", nargs="?", default=None)
    ws_purge.add_argument(
        "--confirm",
        action="store_true",
        help="必须显式带此 flag 才执行;否则拒绝(等价于 service.confirm=True)",
    )
    ws_purge.set_defaults(handler=commands.cmd_workspace_purge)

    ws_destroy = ws_sub.add_parser(
        "destroy",
        help=(
            "完全销毁工作区(三方全删 — 工作区目录 + 工作区内 backups/ + 顶级 "
            "archives/<id>.tar.gz;完全不可逆;P7-B-07 起新增)"
        ),
    )
    ws_destroy.add_argument("workspace_id")
    ws_destroy.add_argument(
        "--confirm",
        required=True,
        help=(
            "必须输入与 workspace_id 完全一致的字符串(strict 二次确认 — destroy "
            "三方全删完全不可逆)"
        ),
    )
    ws_destroy.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    ws_destroy.set_defaults(handler=commands.cmd_workspace_destroy)

    ws_backup = ws_sub.add_parser(
        "backup",
        help="备份工作区到工作区内 backups/<id>-<timestamp>.tar.gz(原目录保留)",
    )
    ws_backup.add_argument("workspace_id", nargs="?", default=None)
    ws_backup.set_defaults(handler=commands.cmd_workspace_backup)

    ws_restore = ws_sub.add_parser(
        "restore",
        help=(
            "从 workspace 内 backups/<backup_id>.zip 恢复工作区(P6-B-07 新语义);"
            "--backup-id 与 --latest 二选一;--confirm 二次输入"
        ),
    )
    ws_restore.add_argument("workspace_id")
    _ws_restore_source = ws_restore.add_mutually_exclusive_group(required=True)
    _ws_restore_source.add_argument(
        "--backup-id",
        dest="backup_id",
        default=None,
        help="指定要恢复的 backup_id(``BackupInfoOut.backup_id`` 字面值)",
    )
    _ws_restore_source.add_argument(
        "--latest",
        action="store_true",
        help="便捷恢复最近一份备份(从 backups 列表第一项取)",
    )
    ws_restore.add_argument(
        "--confirm",
        required=True,
        help="必须输入与 workspace_id 完全一致的字符串(二次确认)",
    )
    ws_restore.add_argument(
        "--json",
        action="store_true",
        help="JSON 单行输出(机器友好);不传走人类可读 key:value 格式",
    )
    ws_restore.set_defaults(handler=commands.cmd_workspace_restore)

    ws_migrate = ws_sub.add_parser(
        "migrate",
        help=(
            "对工作区执行受控数据迁移(pre-migration 备份 + alembic upgrade head + "
            "更新 meta.json);--confirm 二次输入"
        ),
    )
    ws_migrate.add_argument("workspace_id")
    ws_migrate.add_argument(
        "--confirm",
        required=True,
        help="必须输入与 workspace_id 完全一致的字符串(二次确认)",
    )
    ws_migrate.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    ws_migrate.set_defaults(handler=commands.cmd_workspace_migrate)

    ws_version_status = ws_sub.add_parser(
        "version-status",
        help=(
            "查询 workspace meta.json 中 engine_version / schema_version 与"
            "当前引擎的兼容性比对结果(doctor)"
        ),
    )
    ws_version_status.add_argument("workspace_id")
    ws_version_status.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    ws_version_status.set_defaults(
        handler=commands.cmd_workspace_version_status
    )

    ws_backups = ws_sub.add_parser(
        "backups",
        help=(
            "列出工作区最近 30 份自动备份 + 全部手动备份(按 created_at 倒序;"
            "不含内部 pre_restore_*.zip)"
        ),
    )
    ws_backups.add_argument("workspace_id")
    ws_backups.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    ws_backups.set_defaults(handler=commands.cmd_workspace_backups)

    ws_import = ws_sub.add_parser(
        "import-assets",
        help=(
            "从 markdown 目录(world.md / characters.md / plot_threads.md / "
            "timeline.md)解析静态资产入 SQLite + Chroma;同名条目幂等覆盖"
        ),
    )
    ws_import.add_argument(
        "md_dir",
        help="资产目录路径(可包含 4 个 md 文件,均可选)",
    )
    ws_import.add_argument(
        "--workspace",
        default=None,
        help="工作区 ID;缺省读 _active 软链",
    )
    ws_import.set_defaults(handler=commands.cmd_workspace_import_assets)

    # ---- 章节命令 -------------------------------------------------------------
    write = subparsers.add_parser(
        "write-chapter", help="跑一次完整章节闭环(写 → 审 → 修订/重写 → 通过)"
    )
    write.add_argument("chapter_id")
    write.add_argument(
        "--workspace",
        default=None,
        help="工作区 ID;缺省读 _active 软链",
    )
    write.set_defaults(handler=commands.cmd_write_chapter)

    rewrite = subparsers.add_parser(
        "rewrite-chapter", help="强制重写章节(对应 ChapterCard.max_rewrites 名额预算)"
    )
    rewrite.add_argument("chapter_id")
    rewrite.add_argument("--workspace", default=None)
    rewrite.add_argument(
        "--prior-rewrite-count",
        type=int,
        default=0,
        help="已用过的重写次数(对接 max_rewrites 名额预算),默认 0",
    )
    rewrite.set_defaults(handler=commands.cmd_rewrite_chapter)

    revise = subparsers.add_parser(
        "revise-chapter",
        help="基于 ReviewIssue 列表局部修订章节(不走 Orchestrator,不动 final.md)",
    )
    revise.add_argument("chapter_id")
    revise.add_argument("--workspace", default=None)
    revise.add_argument(
        "--issues-file",
        required=True,
        help="JSON 数组路径;每个元素需符合 ReviewIssue schema",
    )
    revise.add_argument(
        "--source-version-id",
        type=int,
        default=None,
        help="修订源版本号;缺省取最新版本",
    )
    revise.set_defaults(handler=commands.cmd_revise_chapter)

    # ---- chapter 子命令组(P7-B-01) -----------------------------------------
    chapter = subparsers.add_parser(
        "chapter",
        help="章节人工介入(rewind / force-pass / unblock / unblock-all)",
    )
    chapter_sub = chapter.add_subparsers(dest="chapter_command", required=True)

    chapter_rewind = chapter_sub.add_parser(
        "rewind",
        help=(
            "rewind 目标章节(回退到 drafting + 后续章节 invalidated + Chroma "
            "soft-delete + extracted_* revoked);强制 --confirm 二次输入"
        ),
    )
    chapter_rewind.add_argument("workspace_id")
    chapter_rewind.add_argument("chapter_id")
    chapter_rewind.add_argument(
        "--confirm",
        required=True,
        help="必须输入与 workspace_id 完全一致的字符串(二次确认)",
    )
    chapter_rewind.set_defaults(handler=commands.cmd_chapter_rewind)

    chapter_force_pass = chapter_sub.add_parser(
        "force-pass",
        help="强制审稿通过(跳过 Reviewer 直接 approved);--reason 必填",
    )
    chapter_force_pass.add_argument("workspace_id")
    chapter_force_pass.add_argument("chapter_id")
    chapter_force_pass.add_argument(
        "--reason",
        required=True,
        help="force-pass 触发理由(对齐 ChapterForcePassIn.reason min_length=1)",
    )
    chapter_force_pass.set_defaults(handler=commands.cmd_chapter_force_pass)

    chapter_unblock = chapter_sub.add_parser(
        "unblock",
        help="单章 unblock(invalidated → drafting)",
    )
    chapter_unblock.add_argument("workspace_id")
    chapter_unblock.add_argument("chapter_id")
    chapter_unblock.set_defaults(handler=commands.cmd_chapter_unblock)

    chapter_unblock_all = chapter_sub.add_parser(
        "unblock-all",
        help="批量 unblock 整个 workspace 内的 invalidated 章节(幂等)",
    )
    chapter_unblock_all.add_argument("workspace_id")
    chapter_unblock_all.set_defaults(handler=commands.cmd_chapter_unblock_all)

    # ---- pipeline 子命令组(P7-B-02) -----------------------------------------
    pipeline = subparsers.add_parser(
        "pipeline",
        help=(
            "流水线运行控制(start / pause / resume / stop / status)"
            "与离线 cap 维护"
        ),
    )
    pipeline_sub = pipeline.add_subparsers(
        dest="pipeline_command", required=True
    )

    pipeline_start = pipeline_sub.add_parser(
        "start",
        help=(
            "启动新流水线 run;scope 4 档(single / range / volume / until);"
            "字段映射约束由 service 层校验"
        ),
    )
    pipeline_start.add_argument("workspace_id")
    pipeline_start.add_argument(
        "--scope",
        required=True,
        choices=["single", "range", "volume", "until"],
        help=(
            "scope 4 档:single 单章 / range 起止区间 / volume 整卷 / "
            "until 起点到指定终点(或所在卷末)"
        ),
    )
    pipeline_start.add_argument(
        "--start",
        default=None,
        help="起始章节 ID(single / range / until 必填,volume 忽略)",
    )
    pipeline_start.add_argument(
        "--end",
        default=None,
        help="结束章节 ID(range 必填;until 可选;single / volume 忽略)",
    )
    pipeline_start.add_argument(
        "--volume-id",
        default=None,
        dest="volume_id",
        help="卷 ID(volume 必填;其他 scope 忽略)",
    )
    pipeline_start.add_argument(
        "--profile",
        default=None,
        help="本次 run 强制使用的 writer profile_id;不传走默认 routing",
    )
    pipeline_start.add_argument(
        "--provider-call-cap",
        type=int,
        default=1000,
        help="本次 run 的 provider 物理调用 hard cap(跨暂停/重启累计)",
    )
    pipeline_start.add_argument(
        "--json",
        action="store_true",
        help="JSON 单行输出(机器友好);不传走人类可读 key:value 格式",
    )
    pipeline_start.set_defaults(handler=commands.cmd_pipeline_start)

    pipeline_pause = pipeline_sub.add_parser(
        "pause",
        help="暂停 active run(自动按 workspace 找 active;幂等)",
    )
    pipeline_pause.add_argument("workspace_id")
    pipeline_pause.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    pipeline_pause.set_defaults(handler=commands.cmd_pipeline_pause)

    pipeline_resume = pipeline_sub.add_parser(
        "resume",
        help="恢复 paused run(paused → running;幂等)",
    )
    pipeline_resume.add_argument("workspace_id")
    pipeline_resume.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    pipeline_resume.set_defaults(handler=commands.cmd_pipeline_resume)

    pipeline_stop = pipeline_sub.add_parser(
        "stop",
        help="终止 active 或 error run(→ stopped)",
    )
    pipeline_stop.add_argument("workspace_id")
    pipeline_stop.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    pipeline_stop.set_defaults(handler=commands.cmd_pipeline_stop)

    pipeline_status = pipeline_sub.add_parser(
        "status",
        help="读 active run 状态;无 active → 最近 1 行;无任何历史 → idle",
    )
    pipeline_status.add_argument("workspace_id")
    pipeline_status.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    pipeline_status.set_defaults(handler=commands.cmd_pipeline_status)

    pipeline_increase_cap = pipeline_sub.add_parser(
        "increase-provider-cap",
        help=(
            "离线维护：完整备份后提高已暂停 run 的 Provider hard cap；"
            "不恢复 run、不发请求"
        ),
    )
    pipeline_increase_cap.add_argument("workspace_id")
    pipeline_increase_cap.add_argument(
        "--run-id",
        required=True,
        help="必须与暂停 checkpoint 精确一致的 run ID",
    )
    pipeline_increase_cap.add_argument(
        "--expected-current-cap",
        required=True,
        type=int,
        help="执行前预期的当前 cap；不一致则零写入硬停",
    )
    pipeline_increase_cap.add_argument(
        "--new-cap",
        required=True,
        type=int,
        help="新 cap；必须更大且单次最多增加 20",
    )
    pipeline_increase_cap.add_argument(
        "--confirm",
        required=True,
        help="精确确认：workspace_id:run_id:旧cap:新cap",
    )
    pipeline_increase_cap.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    pipeline_increase_cap.set_defaults(
        handler=commands.cmd_pipeline_increase_provider_cap
    )

    # ---- export 子命令组(P7-B-03) ------------------------------------------
    export = subparsers.add_parser(
        "export",
        help="导出引擎(run 同步阻塞跑 / list 历史 / download 写盘)",
    )
    export_sub = export.add_subparsers(
        dest="export_command", required=True
    )

    export_run = export_sub.add_parser(
        "run",
        help=(
            "同步阻塞跑一次完整导出(stage 推 stderr,最终 manifest 摘要走 "
            "stdout);format 4 档 + scope 3 档;字段映射约束由 service 层校验"
        ),
    )
    export_run.add_argument("workspace_id")
    export_run.add_argument(
        "--format",
        required=True,
        choices=["md", "chaptered_md", "epub", "docx"],
        help="导出格式 4 档(md 全章拼接 / chaptered_md 每章一文件 / epub / docx)",
    )
    export_run.add_argument(
        "--scope",
        required=True,
        choices=["full", "volume", "range"],
        help="导出范围 3 档(full 全书 / volume 单卷 / range 区间)",
    )
    export_run.add_argument(
        "--volume-id",
        default=None,
        dest="volume_id",
        help="卷 ID(scope=volume 必填;其他 scope 忽略)",
    )
    export_run.add_argument(
        "--start",
        default=None,
        help="起始章节 ID(scope=range 必填;其他 scope 忽略)",
    )
    export_run.add_argument(
        "--end",
        default=None,
        help="结束章节 ID(scope=range 必填;其他 scope 忽略)",
    )
    export_run.add_argument(
        "--include-metadata",
        action="store_true",
        default=True,
        dest="include_metadata",
        help="zip 内含 meta.json(作者/字数/卷章数);默认 True",
    )
    export_run.add_argument(
        "--no-include-metadata",
        action="store_false",
        dest="include_metadata",
        help="zip 不含 meta.json(覆盖默认 True)",
    )
    export_run.add_argument(
        "--include-review-history",
        action="store_true",
        default=False,
        dest="include_review_history",
        help="zip 内附 review_history.json(review_results 表行);默认 False",
    )
    export_run.add_argument(
        "--json",
        action="store_true",
        help="JSON 单行输出(机器友好);不传走人类可读 key:value 格式",
    )
    export_run.add_argument("--no-length-notes", action="store_true",
                            help="纯净导出，不在特别长的章节标题后附编辑提示")
    export_run.set_defaults(handler=commands.cmd_export_run)

    export_list = export_sub.add_parser(
        "list",
        help="列出最近 10 条导出条目(按 created_at 倒序;含 pending/running/done/failed)",
    )
    export_list.add_argument("workspace_id")
    export_list.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    export_list.set_defaults(handler=commands.cmd_export_list)

    export_download = export_sub.add_parser(
        "download",
        help="把指定 export_id 的 zip 文件复制到 --out 路径(自动建父目录)",
    )
    export_download.add_argument("workspace_id")
    export_download.add_argument("export_id")
    export_download.add_argument(
        "--out",
        required=True,
        help="输出文件路径(父目录不存在自动创建)",
    )
    export_download.add_argument(
        "--json", action="store_true", help="JSON 单行输出"
    )
    export_download.set_defaults(handler=commands.cmd_export_download)

    # ---- logs 子命令(P4-B-05) ----------------------------------------------
    logs_cmd = subparsers.add_parser(
        "logs",
        help="日志查询(对齐 OBSERVABILITY §2.6)",
    )
    logs_cmd.add_argument(
        "--chapter", default=None, help="按 chapter_id 过滤"
    )
    logs_cmd.add_argument(
        "--trace", default=None, help="按 trace_id 过滤"
    )
    logs_cmd.add_argument(
        "--request", default=None, help="按 request_id 过滤"
    )
    logs_cmd.add_argument(
        "--event", default=None, help="按 event 名过滤(snake_case)"
    )
    logs_cmd.add_argument(
        "--level",
        default=None,
        help="按 level 过滤(case-insensitive,如 INFO / WARNING / ERROR)",
    )
    logs_cmd.add_argument(
        "--workspace",
        default=None,
        help="按 workspace_id 过滤(缺省读 _active 软链)",
    )
    logs_cmd.add_argument(
        "--since",
        default=None,
        help='起始时间(ISO 8601,如 "2026-05-10T00:00:00Z")',
    )
    logs_cmd.add_argument(
        "--until",
        default=None,
        help='终止时间(ISO 8601,如 "2026-05-10T23:59:59Z")',
    )
    logs_cmd.add_argument(
        "--tail",
        type=int,
        nargs="?",
        const=50,
        default=None,
        help="只显示最近 N 条(默认 50);与其他过滤器组合时先过滤再取尾部",
    )
    logs_cmd.add_argument(
        "--limit",
        type=int,
        default=50,
        help="非 --tail 模式下的输出条数上限,默认 50",
    )
    logs_cmd.set_defaults(handler=commands.cmd_logs)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。``console_scripts`` wrapper 自动 ``sys.exit(main())``。

    ``argv=None`` 时由 argparse 默认从 ``sys.argv[1:]`` 取(常规命令行调用);
    显式传 list 时用于测试(``main(["workspace", "list"])``)。
    """
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return commands.dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
