"""系统健康检查服务(P4-acceptance + P4-acceptance-followup)。

对齐 OBSERVABILITY §6 字面 10 项健康检查。

**全局 6 项**(不依赖 workspace 上下文,默认始终跑):

1. ``config_yaml_loaded`` —— 引擎默认 ``config.yaml`` 可解析
2. ``data_dir_readable`` —— ``settings.data_dir`` 存在 + 可遍历
3. ``logs_dir_writable`` —— 系统级日志目录可创建 + 写探针 + 删除
4. ``ollama_endpoint_reachable`` —— ollama ``/api/tags`` 返 200
   (``LLM_PROVIDER=ollama`` 时;其他 provider skip)
5. ``disk_space_above_1gb`` —— ``settings.data_dir`` 所在磁盘剩余 > 1GB
6. ``engine_version_present`` —— ``ENGINE_VERSION`` 不为空

**P4-acceptance-followup 加 5 项**(``workspace_id`` 给定时跑;未给跳过):

7. ``workspace_sqlite_readwrite`` —— 工作区 ``novel.db`` 可 connect +
   写临时表 + 删除
8. ``workspace_chroma_readwrite`` —— 工作区 ``chroma/`` 存在 + Chroma
   client 可 ``list_collections``
9. ``embedding_model_loadable`` —— ``sentence_transformers`` 可 import
   + Qwen3-Embedding-0.6B 模型缓存目录存在(不实际加载模型避免
   ~1.2GB RAM + 几十秒开销)
10. ``backup_dir_health`` —— 工作区 ``backups/`` 目录可读
11. ``engine_version_compat`` —— ``meta.json.engine_version`` 与当前
    ``ENGINE_VERSION`` 比对(compatible / workspace_older 通过;
    workspace_newer 失败)

**留 v2_wishlist 候选**(P5+ 才做):

- ``claude_api_reachable`` —— 维护人无 API key(CLAUDE.md §维护人环境约束)
- 上述 5 项的多工作区批量遍历(当前只接 ``?workspace_id=`` 单工作区查询)

总判定:全 pass → ``status=ok``;任一 fail 但非关键项 → ``status=degraded``;
关键项(``data_dir_readable`` / ``engine_version_present`` /
``workspace_sqlite_readwrite``)fail → ``status=down``。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, Field

from src.config import settings
from src.core.config_loader import load_writer_profiles_config
from src.core.workspace.workspace_paths import WorkspacePaths
from src.version import ENGINE_VERSION

_log = structlog.get_logger(__name__)

HealthStatus = Literal["ok", "degraded", "down"]
CheckOutcome = Literal["pass", "fail", "skip"]


class HealthCheckItem(BaseModel):
    """单项检查结果。"""

    name: str = Field(description="检查项标识名(snake_case)")
    outcome: CheckOutcome = Field(description="pass | fail | skip")
    detail: str | None = Field(
        default=None, description="附加说明(失败原因 / skip 理由 / pass 度量值)"
    )


_CRITICAL_NAMES = frozenset(
    {
        "data_dir_readable",
        "engine_version_present",
        "workspace_sqlite_readwrite",
    }
)


_ENGINE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


def _check_config_yaml_loaded() -> HealthCheckItem:
    try:
        cfg = load_writer_profiles_config(_ENGINE_CONFIG_PATH)
        profiles_count = len(cfg.profiles or {})
        return HealthCheckItem(
            name="config_yaml_loaded",
            outcome="pass",
            detail=f"profiles={profiles_count}",
        )
    except Exception as exc:  # noqa: BLE001
        return HealthCheckItem(
            name="config_yaml_loaded", outcome="fail", detail=str(exc)[:200]
        )


def _check_data_dir_readable() -> HealthCheckItem:
    path = settings.data_dir
    if not path.exists():
        return HealthCheckItem(
            name="data_dir_readable",
            outcome="fail",
            detail=f"data_dir does not exist: {path}",
        )
    try:
        # 触发 stat + iterdir,验证可遍历
        _ = list(path.iterdir())
        return HealthCheckItem(
            name="data_dir_readable", outcome="pass", detail=str(path)
        )
    except OSError as exc:
        return HealthCheckItem(
            name="data_dir_readable", outcome="fail", detail=str(exc)[:200]
        )


def _check_logs_dir_writable() -> HealthCheckItem:
    logs_dir = settings.data_dir / "system_logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        probe = logs_dir / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return HealthCheckItem(
            name="logs_dir_writable", outcome="pass", detail=str(logs_dir)
        )
    except OSError as exc:
        return HealthCheckItem(
            name="logs_dir_writable", outcome="fail", detail=str(exc)[:200]
        )


def _check_ollama_endpoint_reachable() -> HealthCheckItem:
    if settings.llm_provider != "ollama":
        return HealthCheckItem(
            name="ollama_endpoint_reachable",
            outcome="skip",
            detail=f"LLM_PROVIDER={settings.llm_provider},not ollama",
        )
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            return HealthCheckItem(
                name="ollama_endpoint_reachable",
                outcome="pass",
                detail=f"{len(models)} models available",
            )
        return HealthCheckItem(
            name="ollama_endpoint_reachable",
            outcome="fail",
            detail=f"HTTP {resp.status_code}",
        )
    except Exception as exc:  # noqa: BLE001
        return HealthCheckItem(
            name="ollama_endpoint_reachable",
            outcome="fail",
            detail=str(exc)[:200],
        )


def _check_disk_space_above_1gb() -> HealthCheckItem:
    try:
        usage = shutil.disk_usage(settings.data_dir)
        free_gb = usage.free / (1024**3)
        if free_gb >= 1.0:
            return HealthCheckItem(
                name="disk_space_above_1gb",
                outcome="pass",
                detail=f"{free_gb:.2f} GB free",
            )
        return HealthCheckItem(
            name="disk_space_above_1gb",
            outcome="fail",
            detail=f"{free_gb:.2f} GB free, threshold 1.0 GB",
        )
    except OSError as exc:
        return HealthCheckItem(
            name="disk_space_above_1gb",
            outcome="fail",
            detail=str(exc)[:200],
        )


def _check_engine_version_present() -> HealthCheckItem:
    if ENGINE_VERSION and isinstance(ENGINE_VERSION, str):
        return HealthCheckItem(
            name="engine_version_present",
            outcome="pass",
            detail=ENGINE_VERSION,
        )
    return HealthCheckItem(
        name="engine_version_present",
        outcome="fail",
        detail="ENGINE_VERSION is empty or non-string",
    )


def _check_workspace_sqlite_readwrite(workspace_id: str) -> HealthCheckItem:
    """工作区 SQLite 可读 + 可写临时表 + 删除(P4-acceptance-followup)。"""
    paths = WorkspacePaths(workspace_id)
    db_path = paths.db_path
    if not db_path.exists():
        return HealthCheckItem(
            name="workspace_sqlite_readwrite",
            outcome="fail",
            detail=f"novel.db not found: {db_path}",
        )
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _health_probe (id INTEGER)"
            )
            conn.execute("INSERT INTO _health_probe (id) VALUES (1)")
            conn.execute("DROP TABLE _health_probe")
            conn.commit()
        finally:
            conn.close()
        return HealthCheckItem(
            name="workspace_sqlite_readwrite",
            outcome="pass",
            detail=str(db_path),
        )
    except sqlite3.Error as exc:
        return HealthCheckItem(
            name="workspace_sqlite_readwrite",
            outcome="fail",
            detail=str(exc)[:200],
        )


def _check_workspace_chroma_readwrite(workspace_id: str) -> HealthCheckItem:
    """工作区 Chroma 目录可读写探针。

    v1.0.x wave 5 session 1:目录不存在 → 顺便初始化(``mkdir + PersistentClient``
    探针),成功 pass + detail 标 "auto-initialized";真探针失败(权限错 /
    文件占用 / chromadb 异常)才 fail。**语义对齐 chromadb.PersistentClient
    本身的"path 不存在自动建"行为**,把"批量生成阶段尚未触发任何向量写入
    的合法中间状态"从 fail 改为 pass(对照 luo2 现象:跑过 outline + cards
    但未跑正文/MemoryExtractor → 无 chroma/ 子目录 → 健康检查误报)。

    与 ``workspace_service._create_skeleton`` 的"创建时一并建 chroma_dir"
    防御层互补:① 新工作区从 ``_create_skeleton`` 就有 chroma/(主路径);
    ② 老工作区 / 异常清理过 chroma/ 的工作区 → 本探针顺便补建(兜底路径)。
    """
    paths = WorkspacePaths(workspace_id)
    chroma_dir = paths.chroma_dir
    auto_initialized = False
    if not chroma_dir.exists():
        try:
            chroma_dir.mkdir(parents=True, exist_ok=True)
            auto_initialized = True
        except OSError as exc:
            return HealthCheckItem(
                name="workspace_chroma_readwrite",
                outcome="fail",
                detail=f"chroma dir mkdir failed: {exc}"[:200],
            )
    try:
        import chromadb
        from chromadb.config import Settings

        # v1.0.x A2-01:Settings 与 ``VectorStore`` (src/core/memory/vector.py:130)
        # 一致,避免 ChromaDB SharedSystemClient 同 path 不同 settings 触发
        # "An instance of Chroma already exists ... with different settings"
        # ValueError(health 探针先跑 / VectorStore 后跑场景在同进程 health
        # check + write 链路下必撞)。
        client = chromadb.PersistentClient(
            path=str(chroma_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        cols = client.list_collections()
        detail = f"{len(cols)} collections"
        if auto_initialized:
            detail += " (auto-initialized)"
        return HealthCheckItem(
            name="workspace_chroma_readwrite",
            outcome="pass",
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        return HealthCheckItem(
            name="workspace_chroma_readwrite",
            outcome="fail",
            detail=str(exc)[:200],
        )


def _check_embedding_model_loadable() -> HealthCheckItem:
    """sentence_transformers 可 import + 模型缓存目录存在(不实际加载,
    避免 ~1.2GB RAM + 几十秒开销 + 离线环境首次下载)。"""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError as exc:
        return HealthCheckItem(
            name="embedding_model_loadable",
            outcome="fail",
            detail=f"sentence_transformers import failed: {exc}",
        )
    # HuggingFace cache 命名:models--<org>--<model_name>
    cache_dir = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--Qwen--Qwen3-Embedding-0.6B"
    )
    if not cache_dir.exists():
        return HealthCheckItem(
            name="embedding_model_loadable",
            outcome="fail",
            detail=f"model cache missing: {cache_dir}(首次使用会自动下载)",
        )
    return HealthCheckItem(
        name="embedding_model_loadable",
        outcome="pass",
        detail=f"Qwen3-Embedding-0.6B cache at {cache_dir}",
    )


def _check_backup_dir_health(workspace_id: str) -> HealthCheckItem:
    """工作区 ``backups/`` 目录可读(P4 简版:存在 + 可遍历;P5+ 自动备份
    子系统引入后扩展为"最近备份时间 / 总大小 / 损坏校验")。"""
    paths = WorkspacePaths(workspace_id)
    backups_dir = paths.backups_dir
    if not backups_dir.exists():
        # 工作区刚建出未触发备份是常态,skip 而非 fail
        return HealthCheckItem(
            name="backup_dir_health",
            outcome="skip",
            detail=f"backups dir not yet created: {backups_dir}",
        )
    try:
        entries = list(backups_dir.iterdir())
        return HealthCheckItem(
            name="backup_dir_health",
            outcome="pass",
            detail=f"{len(entries)} backup entries",
        )
    except OSError as exc:
        return HealthCheckItem(
            name="backup_dir_health",
            outcome="fail",
            detail=str(exc)[:200],
        )


def _check_engine_version_compat(workspace_id: str) -> HealthCheckItem:
    """工作区 ``meta.json.engine_version`` 与当前 ``ENGINE_VERSION`` 比对。

    compatible / workspace_older → pass;workspace_newer → fail(应升引擎)。
    meta.json 不存在 / 解析失败 → fail。
    """
    from src.core.workspace.meta import (
        MetaFileError,
        compare_engine_version,
        read_meta,
    )

    paths = WorkspacePaths(workspace_id)
    try:
        meta = read_meta(paths.meta_json)
    except MetaFileError as exc:
        return HealthCheckItem(
            name="engine_version_compat",
            outcome="fail",
            detail=str(exc)[:200],
        )
    try:
        result = compare_engine_version(meta.engine_version)
    except ValueError as exc:
        return HealthCheckItem(
            name="engine_version_compat",
            outcome="fail",
            detail=str(exc)[:200],
        )
    if result == "workspace_newer":
        return HealthCheckItem(
            name="engine_version_compat",
            outcome="fail",
            detail=f"workspace={meta.engine_version} > engine={ENGINE_VERSION}",
        )
    return HealthCheckItem(
        name="engine_version_compat",
        outcome="pass",
        detail=f"workspace={meta.engine_version} engine={ENGINE_VERSION} ({result})",
    )


def run_health_checks(
    *, workspace_id: str | None = None
) -> dict[str, Any]:
    """跑全局 6 项 + 可选工作区 5 项健康检查,返结构化报告。

    :param workspace_id: 给定时跑工作区 5 项;未给则只跑全局 6 项。

    返结构:
    - ``status``: ``ok`` / ``degraded`` / ``down``
    - ``engine_version``: P1 简版字段保留
    - ``checks``: ``list[HealthCheckItem]``
    - ``summary``: ``{pass_count, fail_count, skip_count}``
    """

    checks = [
        _check_config_yaml_loaded(),
        _check_data_dir_readable(),
        _check_logs_dir_writable(),
        _check_ollama_endpoint_reachable(),
        _check_disk_space_above_1gb(),
        _check_engine_version_present(),
    ]
    if workspace_id is not None:
        checks.extend(
            [
                _check_workspace_sqlite_readwrite(workspace_id),
                _check_workspace_chroma_readwrite(workspace_id),
                _check_embedding_model_loadable(),
                _check_backup_dir_health(workspace_id),
                _check_engine_version_compat(workspace_id),
            ]
        )
    pass_count = sum(1 for c in checks if c.outcome == "pass")
    fail_count = sum(1 for c in checks if c.outcome == "fail")
    skip_count = sum(1 for c in checks if c.outcome == "skip")

    failed_names = {c.name for c in checks if c.outcome == "fail"}
    status: HealthStatus
    if failed_names & _CRITICAL_NAMES:
        status = "down"
    elif fail_count > 0:
        status = "degraded"
    else:
        status = "ok"

    _log.info(
        "health_check_run",
        status=status,
        pass_count=pass_count,
        fail_count=fail_count,
        skip_count=skip_count,
    )

    return {
        "status": status,
        "engine_version": ENGINE_VERSION,
        "checks": [c.model_dump() for c in checks],
        "summary": {
            "pass_count": pass_count,
            "fail_count": fail_count,
            "skip_count": skip_count,
        },
    }
