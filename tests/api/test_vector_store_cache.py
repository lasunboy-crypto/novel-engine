"""v1.0.x A2-01:``src/api/deps.py`` VectorStore 工作区级缓存单测。

覆盖:

- 同工作区连续调 factory 两次 → 同一实例(缓存命中)
- 不同工作区两次调 → 两个独立实例(缓存 key 隔离)
- ``reset_vector_store_cache(workspace_id)`` 后再调 → 新实例
- ``reset_vector_store_cache(None)`` 清全部
- workspace_service.destroy / purge / archive / restore 之后缓存被清
- ChromaDB 同 path 多次实例化场景实测不抛 ValueError(根因复现 + 修后验证)

mock 设计:用真 chromadb + tmp_path 物理路径(不 mock chromadb 本身),
embedding fn 用查表 mock 避开 sentence_transformers 加载。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api.deps import (
    _VECTOR_STORE_CACHE,
    _make_vector_store_factory,
    reset_vector_store_cache,
)
from src.core.memory.vector import VectorStore


@pytest.fixture(autouse=True)
def _clear_cache_each_test() -> None:
    """每测试前清空缓存,避免测试间串扰。"""
    _VECTOR_STORE_CACHE.clear()
    yield
    _VECTOR_STORE_CACHE.clear()


@pytest.fixture
def workspaces_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """创建 tmp workspaces 根,patch ``embedding`` 模块返常量 mock embed。"""

    def _mock_embed(texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0, 1.0] for _ in texts]

    monkeypatch.setattr("src.core.memory.embedding.embed", _mock_embed)
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


def _ensure_chroma_dir(workspaces_root: Path, workspace_id: str) -> None:
    """模拟工作区目录结构,chromadb PersistentClient 会自己 mkdir,但显式建避免
    factory 内部 WorkspacePaths 拼路径出错。"""
    (workspaces_root / workspace_id / "data" / "chroma").mkdir(parents=True)


# =============================================================================
# 基础缓存行为
# =============================================================================


def test_cache_hit_returns_same_instance(workspaces_root: Path) -> None:
    """同工作区连续调 factory 两次 → 返回同一 VectorStore 实例。"""
    _ensure_chroma_dir(workspaces_root, "ws1")
    factory = _make_vector_store_factory(workspaces_root)

    first = factory("ws1")
    second = factory("ws1")

    assert first is second
    assert isinstance(first, VectorStore)


def test_cache_miss_for_different_workspaces(workspaces_root: Path) -> None:
    """不同工作区两次调 → 两个独立实例(缓存 key 工作区级隔离)。"""
    _ensure_chroma_dir(workspaces_root, "ws1")
    _ensure_chroma_dir(workspaces_root, "ws2")
    factory = _make_vector_store_factory(workspaces_root)

    store1 = factory("ws1")
    store2 = factory("ws2")

    assert store1 is not store2
    assert store1.workspace_id == "ws1"
    assert store2.workspace_id == "ws2"


def test_reset_specific_workspace_creates_new_instance(
    workspaces_root: Path,
) -> None:
    """``reset_vector_store_cache(ws_id)`` 后再调 factory → 新实例。"""
    _ensure_chroma_dir(workspaces_root, "ws1")
    _ensure_chroma_dir(workspaces_root, "ws2")
    factory = _make_vector_store_factory(workspaces_root)

    before = factory("ws1")
    other = factory("ws2")
    reset_vector_store_cache("ws1")
    after = factory("ws1")
    other_again = factory("ws2")

    assert after is not before
    assert other_again is other  # ws2 缓存未受影响


def test_reset_all_clears_full_cache(workspaces_root: Path) -> None:
    """``reset_vector_store_cache(None)`` 清全部。"""
    _ensure_chroma_dir(workspaces_root, "ws1")
    _ensure_chroma_dir(workspaces_root, "ws2")
    factory = _make_vector_store_factory(workspaces_root)

    factory("ws1")
    factory("ws2")
    assert len(_VECTOR_STORE_CACHE) == 2

    reset_vector_store_cache(None)
    assert len(_VECTOR_STORE_CACHE) == 0


def test_reset_unknown_workspace_is_noop(workspaces_root: Path) -> None:
    """清不存在的 workspace_id 不抛错(幂等)。"""
    reset_vector_store_cache("nonexistent")  # 不抛错
    assert _VECTOR_STORE_CACHE == {}


# =============================================================================
# ChromaDB 同 path 多次实例化场景(根因复现 + 修后验证)
# =============================================================================


def test_repeated_factory_calls_no_chromadb_value_error(
    workspaces_root: Path,
) -> None:
    """根因复现:无缓存时 ChromaDB PersistentClient 同 path 第二次实例化抛
    ValueError;有缓存后第二次走 cache hit 路径,完全绕开第二次实例化。
    """
    _ensure_chroma_dir(workspaces_root, "ws1")
    factory = _make_vector_store_factory(workspaces_root)

    # 修后:连续 3 次调 factory,缓存命中 → 不抛 ValueError
    first = factory("ws1")
    second = factory("ws1")
    third = factory("ws1")

    assert first is second is third


def test_health_check_and_factory_share_chroma_path_no_settings_conflict(
    workspaces_root: Path,
) -> None:
    """A2-01 + health_service.py:248 Settings 统一:health check 用 PersistentClient
    探针 + factory 创 VectorStore 同 path 不抛 "An instance of Chroma already exists
    ... with different settings"。
    """
    import chromadb
    from chromadb.config import Settings

    _ensure_chroma_dir(workspaces_root, "ws1")

    # 模拟 health_service.py:248 路径 — 探针 list_collections
    chroma_dir = workspaces_root / "ws1" / "data" / "chroma"
    probe_client = chromadb.PersistentClient(
        path=str(chroma_dir),
        settings=Settings(anonymized_telemetry=False),
    )
    probe_client.list_collections()

    # 然后 factory 在同进程内同 path 再造 VectorStore — 不抛错
    factory = _make_vector_store_factory(workspaces_root)
    store = factory("ws1")

    assert isinstance(store, VectorStore)
    assert store.workspace_id == "ws1"


# =============================================================================
# WorkspaceService 生命周期方法触发缓存清失效
# =============================================================================


def test_workspace_destroy_resets_cache(workspaces_root: Path) -> None:
    """``WorkspaceService.destroy`` 调 ``_reset_vector_store_cache(workspace_id)``。"""
    from src.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(workspaces_root=workspaces_root)
    ws_service.create(
        "ws1",
        display_name="test",
        genre="test",
        premise="test",
        activate=False,
    )
    factory = _make_vector_store_factory(workspaces_root)
    factory("ws1")  # 占用缓存
    assert "ws1" in _VECTOR_STORE_CACHE

    ws_service.destroy("ws1", confirm="ws1")
    assert "ws1" not in _VECTOR_STORE_CACHE


def test_workspace_purge_resets_cache(workspaces_root: Path) -> None:
    """``purge`` 同 destroy,清缓存。"""
    from src.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(workspaces_root=workspaces_root)
    ws_service.create(
        "ws1",
        display_name="test",
        genre="test",
        premise="test",
        activate=False,
    )
    factory = _make_vector_store_factory(workspaces_root)
    factory("ws1")
    assert "ws1" in _VECTOR_STORE_CACHE

    ws_service.purge("ws1", confirm=True)
    assert "ws1" not in _VECTOR_STORE_CACHE


def test_workspace_archive_resets_cache(workspaces_root: Path) -> None:
    """``archive`` 也清缓存(chroma 路径在打包后 rmtree)。"""
    from src.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(
        workspaces_root=workspaces_root,
        archives_root=workspaces_root.parent / "archives",
    )
    ws_service.create(
        "ws1",
        display_name="test",
        genre="test",
        premise="test",
        activate=False,
    )
    factory = _make_vector_store_factory(workspaces_root)
    factory("ws1")
    assert "ws1" in _VECTOR_STORE_CACHE

    ws_service.archive("ws1")
    assert "ws1" not in _VECTOR_STORE_CACHE


def test_workspace_restore_resets_cache(workspaces_root: Path) -> None:
    """``restore`` 从 archive 解出后清缓存。"""
    from src.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(
        workspaces_root=workspaces_root,
        archives_root=workspaces_root.parent / "archives",
    )
    ws_service.create(
        "ws1",
        display_name="test",
        genre="test",
        premise="test",
        activate=False,
    )
    ws_service.archive("ws1")  # 此时 cache 已被 archive 清空

    # archive 后工作区目录已不存在,模拟"陈旧缓存残留"场景(如果 archive 没清)
    _VECTOR_STORE_CACHE["ws1"] = object()  # type: ignore[assignment]

    ws_service.restore("ws1")
    assert "ws1" not in _VECTOR_STORE_CACHE
