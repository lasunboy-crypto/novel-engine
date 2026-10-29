"""工作区 ``meta.json`` 读写与引擎版本比对。

PROJECT_SPEC §11.1 要求:启动时把工作区 ``meta.json`` 中的 ``engine_version``
与当前引擎版本(``src.version.ENGINE_VERSION``)比对,决定是否触发迁移。

本模块只负责"读 / 写 / 比对",**不实施迁移**(迁移留给 P1-B-16 alembic 与
未来阶段)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from src.core.models.workspace import Workspace
from src.version import ENGINE_VERSION

VersionCompareResult = Literal["compatible", "workspace_older", "workspace_newer"]


class MetaFileError(Exception):
    """``meta.json`` 读写或解析失败时抛出。

    P1-B-17 全局错误体系起来后会升级为 ``ApiError`` 子类,目前先用普通 Exception。
    """


def read_meta(meta_path: Path) -> Workspace:
    """从 ``meta_path`` 读 JSON 并解析成 ``Workspace``。

    失败场景统一抛 ``MetaFileError`` 并附带清晰中文消息,避免上层拿到裸的
    ``FileNotFoundError`` / ``json.JSONDecodeError`` / Pydantic ``ValidationError``
    后还得自己判错。
    """

    try:
        text = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MetaFileError(f"meta.json 不存在: {meta_path}") from exc
    except OSError as exc:
        raise MetaFileError(f"meta.json 读取失败: {meta_path}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MetaFileError(
            f"meta.json 内容不是合法 JSON: {meta_path} ({exc.msg})"
        ) from exc

    try:
        return Workspace.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError 等
        raise MetaFileError(
            f"meta.json 字段不符合 Workspace schema: {meta_path}"
        ) from exc


def write_meta(meta_path: Path, workspace: Workspace) -> None:
    """把 ``workspace`` 序列化成 JSON 原子写入 ``meta_path``。

    原子写策略:先写到 ``<path>.json.tmp``,再 ``Path.replace`` 覆盖目标。
    POSIX 上 ``rename`` 保证原子性,中途崩溃不会留半截 meta.json。
    """

    payload = workspace.model_dump_json(indent=2)
    tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(meta_path)


def _parse_version(version: str) -> tuple[int, int, int]:
    """把 ``"X.Y.Z"`` 解析成 ``(X, Y, Z)``;非该格式抛 ``ValueError``。

    P1 仅支持三段纯数字版本号(对齐 ``ENGINE_VERSION = "0.1.0"``);出现
    ``"0.1.0a1"`` 之类的预发布后缀直接拒绝,避免静默走错分支。
    """

    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"版本号必须是 X.Y.Z 三段格式,收到 {version!r}(P1 仅支持纯数字版本)"
        )
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise ValueError(
            f"版本号每段必须是非负整数,收到 {version!r}"
        ) from exc


def compare_engine_version(
    workspace_version: str,
    *,
    current_version: str | None = None,
) -> VersionCompareResult:
    """比对工作区版本与当前引擎版本。

    返回三档结论:
      - ``"compatible"``:两者相等
      - ``"workspace_older"``:工作区版本更旧,**可迁移**
      - ``"workspace_newer"``:工作区版本更新,**应升级引擎或拒绝打开**

    ``current_version`` 默认从 ``src.version.ENGINE_VERSION`` 取,允许测试显式
    传入解耦。
    """

    engine = current_version if current_version is not None else ENGINE_VERSION
    ws_tuple = _parse_version(workspace_version)
    engine_tuple = _parse_version(engine)

    if ws_tuple == engine_tuple:
        return "compatible"
    if ws_tuple < engine_tuple:
        return "workspace_older"
    return "workspace_newer"
