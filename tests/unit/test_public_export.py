"""公开导出的隐私边界与可复现性；全部使用临时 Git 仓库。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.export_public import export_public


def _commit(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    files = {
        "README.md": "公开说明\n", "LICENSE": "MIT\n", "pyproject.toml": "[project]\n",
        "config/public_mock.yaml": "writer_routing: {default: mock}\n",
        "docs/open-source/AGENTS.public.md": "公开贡献纪律\n",
        "src/main.py": "answer = 42\n",
        "tests/conftest.py": 'pytest_plugins = ["tests.historical_contract_replay"]\n',
        "tests/unit/test_portable.py": "def test_ok(): assert True\n",
        "tests/unit/test_pinned.py": "def test_old(): assert True\n",
        "tests/unit/test_depends_private.py": "from tests.unit.test_pinned import test_old\n",
        "tests/unit/test_transitive.py": "from tests.unit.test_depends_private import test_old\n",
        "fixtures/past_generated.json": '{"prose": "private output"}\n',
        "tests/fixtures/past_generated.json": '{"prose": "private output"}\n',
        "scripts/past_experiment.py": "private_output = 'keep local'\n",
        "tests/fixtures/historical_contract_revisions.json": json.dumps({"tests/unit/test_pinned.py::test_old": "old"}),
        "docs/acceptance/evidence/private.txt": "private\n",
        "workspaces/book/story.md": "private story\n",
        ".env": "PRIVATE_VALUE=not-for-public\n",
        ".env.local": "PRIVATE_VALUE=also-private\n",
        "config/local.yaml": "private config\n",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    _commit(root)
    return root


def test_exports_committed_code_without_history_or_private_material(source: Path, tmp_path: Path) -> None:
    (source / "src/untracked.py").write_text("do not publish\n")
    (source / "src/main.py").write_text("uncommitted change\n")
    output = tmp_path / "public"
    manifest = export_public(source, output)
    assert (output / "src/main.py").read_text() == "answer = 42\n"
    assert (output / "AGENTS.md").read_text() == "公开贡献纪律\n"
    for private in (".git", ".env", ".env.local", "workspaces", "docs/acceptance", "config/local.yaml", "src/untracked.py", "tests/unit/test_pinned.py", "tests/unit/test_depends_private.py", "tests/unit/test_transitive.py", "fixtures", "tests/fixtures", "scripts/past_experiment.py"):
        assert not (output / private).exists()
    assert b"historical_contract_replay" not in (output / "tests/conftest.py").read_bytes()
    assert manifest["history_included"] is False
    assert "src/main.py" in manifest["files"]
    assert json.loads((output / "PUBLIC_MANIFEST.json").read_text()) == manifest


def test_rejects_credential_without_creating_output(source: Path, tmp_path: Path) -> None:
    (source / "src/leak.py").write_text('api_key = "sk-' + "a" * 40 + '"\n')
    _commit(source)
    output = tmp_path / "public"
    with pytest.raises(ValueError, match="凭据扫描"):
        export_public(source, output)
    assert not output.exists()


def test_rejects_symlink_without_reading_its_target(source: Path, tmp_path: Path) -> None:
    (source / "src/link.py").symlink_to("/a/private/path/that/does/not/exist")
    _commit(source)
    output = tmp_path / "public"
    with pytest.raises(ValueError, match="拒绝链接"):
        export_public(source, output)
    assert not output.exists()


def test_never_overwrites_existing_directory(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "public"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="禁止覆盖"):
        export_public(source, output)
    assert sentinel.read_text() == "keep"


def test_rejects_output_inside_source(source: Path) -> None:
    with pytest.raises(ValueError, match="源码目录之外"):
        export_public(source, source / "public")


def test_excludes_imports_of_private_experimental_scripts(source: Path, tmp_path: Path) -> None:
    path = source / "tests/unit/test_runner_dependency.py"
    path.write_text("from scripts.past_experiment import private_output\n")
    _commit(source)
    output = tmp_path / "public"
    export_public(source, output)
    assert not (output / "scripts/past_experiment.py").exists()
    assert not (output / "tests/unit/test_runner_dependency.py").exists()


def test_public_snapshot_can_be_exported_again(source: Path, tmp_path: Path) -> None:
    first = tmp_path / "public-first"
    first_manifest = export_public(source, first)
    subprocess.run(["git", "init", "-q", str(first)], check=True)
    _commit(first)
    second_manifest = export_public(first, tmp_path / "public-second")
    assert second_manifest["files"] == first_manifest["files"]
    assert second_manifest["snapshot_digest"] == first_manifest["snapshot_digest"]
