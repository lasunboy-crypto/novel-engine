"""I1(v1.1 wave 4 session 3):WorkspaceService 据风格模板生成 style_guide.md 单测。

覆盖 ``WorkspaceService.create`` 的 ``style_template`` 入参:
选定模板 → 写工作区 ``prompts/style_guide.md`` + meta.json 记 style_template;
None / 非法值 → 不写文件(向后兼容)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.workspace_service import WorkspaceService


@pytest.fixture
def service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


def _payload(**overrides):
    base = dict(
        display_name="风格测试本",
        genre="fantasy",
        premise="少年踏上修行路",
        total_chapters=60,
    )
    base.update(overrides)
    return base


def _style_guide_path(tmp_path: Path, workspace_id: str) -> Path:
    return (
        tmp_path / "workspaces" / workspace_id / "prompts" / "style_guide.md"
    )


def test_create_with_template_writes_style_guide(
    service: WorkspaceService, tmp_path: Path
) -> None:
    ws = service.create(
        "ws-comedic", **_payload(), style_template="comedic_burst"
    )
    assert ws.style_template == "comedic_burst"
    guide = _style_guide_path(tmp_path, "ws-comedic")
    assert guide.is_file()
    text = guide.read_text(encoding="utf-8")
    assert "搞笑爽文" in text


def test_create_without_template_no_file(
    service: WorkspaceService, tmp_path: Path
) -> None:
    ws = service.create("ws-plain", **_payload())
    assert ws.style_template is None
    assert not _style_guide_path(tmp_path, "ws-plain").exists()


def test_create_invalid_template_rejected(
    service: WorkspaceService, tmp_path: Path
) -> None:
    # 非法模板名(枚举外)在 Workspace 模型 Literal 校验处被拒(类型安全)。
    # 端点层 schema 同样挡非法枚举(422);此处覆盖 service 层硬校验。
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        service.create(
            "ws-bad", **_payload(), style_template="not_a_template"
        )


def test_generated_guide_matches_template_source(
    service: WorkspaceService, tmp_path: Path
) -> None:
    """生成的 style_guide.md 与引擎级模板正文逐字一致。"""
    from src.config import settings

    service.create(
        "ws-xianxia", **_payload(), style_template="classical_xianxia"
    )
    src = (
        settings.prompts_dir.resolve()
        / "style_guide_templates"
        / "classical_xianxia.md"
    ).read_text(encoding="utf-8")
    dest = _style_guide_path(tmp_path, "ws-xianxia").read_text(encoding="utf-8")
    assert dest == src
