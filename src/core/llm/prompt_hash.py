"""Prompt 哈希计算与原文归档(P1-B-24)。

对齐 OBSERVABILITY §2.5 / §5:

- ``hash_prompt(messages)``:计算 messages 序列的 SHA256 前 12 位 hex。
  落到 ``llm_request`` / ``llm_response`` 事件的 ``prompt_hash`` 字段,
  让运维通过 hash 串联到归档原文。
- ``archive_prompt(workspaces_root, workspace_id, hash, content, provider)``:
  把 prompt 原文落到
  ``workspaces/<id>/archives/prompts/{hash}.txt``。``provider == "mock"``
  时直接 return,不污染 archives 目录(决策:mock 阶段无 prompt 调优需求)。

P1 范围决策(决策 3 折中方案,见 docs/impl/p1_b_24_observability.md):
- 只落 LLM client 层最终 prompt(system + user 拼好之后),不落 agent
  中间产物;真 LLM 阶段 prompt 调优可回查内容。
- 命名空间:``archives/prompts/`` 按 hash 全局池,与 P2 完整 archives 的
  ``archives/<chapter_id>/{rewrite_0,revise_0,...}/`` 子目录互不冲突。
- 已存在同 hash 文件不重写(content 相同则文件相同;不同 prompt 哈希到
  同一 hash 概率极低且本身是哈希碰撞,P1 简版不处理)。
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import structlog

from src.core.llm.base import Message

# SHA256 前 12 位 hex(对齐 OBSERVABILITY §2.5 字面)。
_HASH_PREFIX_LEN = 12

# 按 provider 分类:``"mock"`` 不落原文;真 provider("claude" / 未来的
# "qwen" / "openai")落原文。
_NON_ARCHIVING_PROVIDERS: frozenset[str] = frozenset({"mock"})

_log = structlog.get_logger(__name__)


def hash_prompt(messages: list[Message]) -> str:
    """计算 messages 序列的 prompt hash(SHA256 前 12 位 hex)。

    序列化方案:每条消息按 ``"{role}\\n{content}"`` 转字符串,消息间用
    ``\\n---\\n`` 分隔,再 UTF-8 编码喂 SHA256。这样:

    - 调换两条 message 顺序会得到不同 hash(顺序敏感);
    - role 不同(system/user/assistant)会得到不同 hash;
    - 完全相同的 messages 序列稳定哈希到同一值,跨进程跨机器一致。

    P1 不引入 JSON 序列化(避免 dict key 顺序差异引发误差);P3 引入更复杂
    的 messages 结构(tool_use / multimodal)时再升序列化方案。

    Args:
        messages: ``LLMClient.complete`` 收到的消息序列。

    Returns:
        SHA256 hex 摘要的前 12 位字符串(如 ``"a1b2c3d4e5f6"``)。
    """
    parts = [f"{msg.role}\n{msg.content}" for msg in messages]
    canonical = "\n---\n".join(parts).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:_HASH_PREFIX_LEN]


def archive_prompt(
    *,
    workspaces_root: Path,
    workspace_id: str,
    prompt_hash: str,
    content: str,
    provider: str,
) -> Path | None:
    """把 prompt 原文落到 ``workspaces/<id>/archives/prompts/{hash}.txt``。

    ``provider == "mock"`` 时直接返回 ``None``,不落盘——mock 阶段无 prompt
    调优需求,避免 P1 验收期间 archives 目录被无意义文件污染。其他 provider
    (P1 阶段是 ``"claude"``;P3 起 ``"qwen"`` / ``"openai"``)正常落盘。

    幂等:目标文件已存在则直接返回路径,不重写。同 ``content`` 哈希到同
    ``prompt_hash``,文件内容一致;若出现哈希碰撞(不同 content 同 hash),
    P1 简版接受先落者占位。

    Args:
        workspaces_root: 工作区根目录(含所有工作区子目录的父目录;
            通常是 ``settings.data_dir``)。
        workspace_id: 当前章节所属工作区 ID。
        prompt_hash: ``hash_prompt`` 算出的 12 位 hex,用作文件名。
        content: prompt 原文(LLM client 已脱敏处理,系统级敏感字段不
            进 prompt;§2.5 脱敏只针对日志字段,prompt 本身就是 LLM 输入)。
        provider: provider 标识字符串,``"claude"`` / ``"mock"`` / 未来的
            ``"qwen"`` / ``"openai"``。

    Returns:
        ``provider == "mock"`` 时返回 ``None``;否则返回归档文件路径。
    """
    if provider in _NON_ARCHIVING_PROVIDERS:
        return None

    archive_dir = workspaces_root / workspace_id / "archives" / "prompts"
    archive_file = archive_dir / f"{prompt_hash}.txt"

    if archive_file.exists():
        return archive_file

    # v1.0.x wave 6 s2.6a:race 防御 — 工作区根目录已被 rmtree → swallow + log,
    # 不重建子目录(prompt 归档是 observability 辅助路径,丢失可接受)。
    workspace_root = workspaces_root / workspace_id
    if not workspace_root.is_dir():
        _log.warning(
            "workspace_writer_skipped",
            writer="archive_prompt",
            workspace_id=workspace_id,
            prompt_hash=prompt_hash,
            reason="workspace_destroyed_during_async_task",
        )
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    # 原子写:写**唯一临时文件** → replace 覆盖目标(避免半截文件 + 并发同 hash 竞态)。
    # **唯一临时名(2026-06-17 修)**:并发请求可能同时归档同一份 prompt,
    # 它们 prompt 完全相同 → 同 prompt_hash → 同 archive_file;若共享固定 `<hash>.txt.tmp`,
    # 一个线程 replace 消费掉 tmp 后另一线程 replace 找不到 tmp → FileNotFoundError 崩整章
    # (W3.5-s3b 生成验证 ch-0003 实测撞)。各写各自 `<hash>.<pid>.<tid>.tmp`,replace 原子
    # (同 hash 内容相同,后者覆盖无害)。
    # **fail-soft**:归档是 observability 辅助路径(对齐上方 workspace_destroyed 同款立场),
    # 任何 IO 异常只 warn 不抛 —— 绝不让辅助路径崩掉主流程章写入。
    tmp_file = archive_dir / f"{prompt_hash}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        tmp_file.write_text(content, encoding="utf-8")
        tmp_file.replace(archive_file)
    except Exception:  # noqa: BLE001 — 归档失败不阻断主流程(observability 辅助路径)
        try:
            tmp_file.unlink(missing_ok=True)  # 清理半截 tmp,不残留
        except OSError:
            pass
        _log.warning(
            "workspace_writer_skipped",
            writer="archive_prompt",
            workspace_id=workspace_id,
            prompt_hash=prompt_hash,
            reason="archive_write_failed",
        )
        return None
    return archive_file


def render_prompt_text(messages: list[Message]) -> str:
    """把 messages 序列拼成可读 plaintext,供 ``archive_prompt`` 落盘用。

    格式::

        ## role: system
        <content>

        ---

        ## role: user
        <content>

    与 ``hash_prompt`` 的序列化方案不同——后者求哈希一致性最重要(``role\\n
    content`` 简洁),前者求人读友好(markdown header 结构)。两者各自独立。
    """
    blocks = [f"## role: {msg.role}\n\n{msg.content}" for msg in messages]
    return "\n\n---\n\n".join(blocks)
