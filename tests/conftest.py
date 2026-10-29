# pytest 共用 fixture(临时工作区等)

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr



@pytest.fixture
def valid_character_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """给不以角色准入为测试目标的服务集成测提供合法角色权威记录。

    R9 起空角色表 + 有角色引用必须在 Writer 前失败。旧水位/版本/成本测试的
    章节卡固定引用 ``林衍`` 与 ``lin-yan``，却从未建立角色数据；这些用例应
    显式声明正常前置，而不是让生产 gate fail-open。专门测试空表/坏引用的文件
    不启用本 fixture。
    """
    records: list[MagicMock] = []
    for character_id in ("林衍", "lin-yan"):
        character = MagicMock()
        character.character_id = character_id
        character.name = "林衍"
        character.current_state = {}
        character.voice_profile.forbidden_phrases = []
        character.identity_facets = []
        records.append(character)

    def _list_characters(*_args: object) -> list[MagicMock]:
        return records

    monkeypatch.setattr(
        "src.services.chapter_service.list_characters_by_workspace",
        _list_characters,
    )
    monkeypatch.setattr(
        "src.core.orchestrator.chapter_orchestrator.list_characters_by_workspace",
        _list_characters,
    )
    monkeypatch.setattr(
        "src.core.agents.reference_validator.load_known_ids_from_workspace",
        lambda *_args: ({"林衍", "lin-yan"}, set()),
    )


@pytest.fixture(autouse=True)
def _patch_dashscope_api_key_for_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.0.x wave 6 s2.5 hotfix(2026-05-25):主仓 config.yaml default
    profile 切 ``cloud_qwen_max_standard``(provider=openai_compatible)后,
    任何 bootstrap LLM client 构造(``src/api/deps.py`` 5 处 + ``build_pipeline_components_for_lifespan``)
    走 ``OpenAICompatibleClient`` → ``__init__`` 校验 ``settings.dashscope_api_key``
    非空字符串(``src/core/llm/openai_compatible_client.py:265``)→ 测试环境无 key
    抛 ``ValueError`` 中断流程。

    全局 ``autouse=True`` 给所有测试灌一个非空假 key,让 ``OpenAICompatibleClient``
    构造通过;真实 HTTP 请求由各测试自己的 mock / fixture 拦截(``MockTransport`` /
    ``respx`` / 等),假 key 不会真打到 dashscope endpoint。

    需测"无 key 应抛"的测试(典型 ``tests/unit/test_llm_factory.py``)用自己的
    ``clean_env`` / ``monkeypatch.setattr(settings, "dashscope_api_key", SecretStr(""))``
    在测试内覆盖即可——本 fixture 是 ``autouse`` 但**可被局部 monkeypatch 覆盖**。

    模式参考:``tests/unit/test_llm_factory.py:173`` ``SecretStr("")``。
    """
    from src.config import settings as _settings

    monkeypatch.setattr(
        _settings,
        "dashscope_api_key",
        SecretStr("test-key-placeholder"),
    )


@pytest.fixture(autouse=True)
def _isolate_task_registry(tmp_path: Path) -> Iterator[None]:
    """v1.1 wave 3 session 5(E3):每测试隔离全局 task 持久化 db。

    E3 把 ``task_registry`` 从进程内 LRU 换成全局 ``settings.data_dir/_tasks.db``
    SQLite。若不隔离,单测调 ``record_* / clear`` 会写真实 ``./workspaces/_tasks.db``
    → 跨用例污染 + 真实文件副作用。本 autouse fixture 给每个测试注入 tmp 工厂,
    teardown 复位。

    根 conftest autouse(本 fixture)在同 scope 下早于各测试文件局部
    ``_clear_task_registry`` fixture 运行,故后者 ``clear()`` 命中 tmp db 而非默认。
    """
    from src.api import task_registry
    from src.services.task_db import make_task_session_factory

    task_registry.set_session_factory(
        make_task_session_factory(data_dir=tmp_path / "task_registry")
    )
    yield
    task_registry.reset_session_factory()
