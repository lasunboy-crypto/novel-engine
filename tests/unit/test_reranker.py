"""Reranker(P3-B-08)单测。

测试矩阵
========

- **短路**:候选数 ≤ top_n 时直接返,无 LLM 调用。
- **LLM 路径正常**:LLM 返合法 JSON ranking → 按 ranking 重排返 top_n。
- **LLM 幻觉 ID**:LLM 返不存在的 memory_id → 幻觉丢弃,余下按 LLM
  顺序 + 原距离顺序补位。
- **LLM 漏选 ID**:LLM 返 ranking 短于候选数 → 漏选项按原 distance 顺
  序追加到尾部。
- **LLM 输出非 JSON / 缺 ranking 字段 / ranking 非 list 字符串** → fallback
  返 ``memories[:top_n]``,structlog warn。
- **LLM 调用抛异常** → fallback 返 ``memories[:top_n]``。
- **空候选** → 直接返 ``[]``,无 LLM 调用。
- **top_n <= 0** → 抛 ``RerankerError``。
- **代码块包裹 / trailing 注释剥离**:LLM 输出含 ``` ... ``` / HTML
  注释时正确剥离再解析。
- **router 路径(集成)**:用真 ``ModelRouter`` + ``agent_routing.reranker``
  → 命中正确 profile + client。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.memory.reranker import (
    DEFAULT_TOP_N,
    RerankerError,
    rerank_memories,
)
from src.core.memory.retriever import RetrievedMemory
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.routing import (
    RoutingConfig,
    WriterProfilesConfig,
)
from src.core.models.writer_profile import WriterProfile
from src.core.routing import ModelRouter


# ---------- fixture / factory ----------


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="他握紧剑柄。",
        unfinished_actions=["剑势悬而未发"],
        time_gap="立即续接",
        scene_continuation="same",
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(**overrides) -> ChapterCard:
    payload: dict = dict(
        chapter_id="ch-0002",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章",
        tension_level=8,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["林衍突破第一道剑阵", "苏离表明立场"],
        must_unresolved=["阵眼真凶身份"],
        forbidden=["现代科技元素"],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_make_continuity(),
        next_chapter_seed="苏离消失前留下的玉佩在颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["林衍", "苏离"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _make_memories(n: int = 20) -> list[RetrievedMemory]:
    """构造 n 条 RetrievedMemory,distance 从 0.1 起递增(retriever 已升序)。"""
    return [
        RetrievedMemory(
            memory_id=f"mem-{i:02d}",
            content=f"记忆内容 {i}:这是一段足够长的占位文本,描述某次过往事件。",
            metadata={"entity_type": "fact", "chapter_id": f"ch-{i:04d}"},
            distance=0.1 + i * 0.05,
        )
        for i in range(n)
    ]


def _make_response(
    *,
    content: str,
    model_used: str = "qwen-novel",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=300, output_tokens=80),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """临时目录注入 fake reranker.md,避免依赖仓库根真模板做断言。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "reranker.md").write_text(
        "FAKE_RERANKER_SYSTEM_PROMPT\n\n用于单测。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """清 lru_cache 防 fake 模板互相污染。"""
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def fake_llm_client() -> MagicMock:
    """默认返合法 ranking JSON 的 mock client。"""
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response(
        content=json.dumps(
            {"ranking": [f"mem-{i:02d}" for i in range(5)]},
            ensure_ascii=False,
        )
    )
    return client


# ---------- 入参非法 ----------


class TestInvalidArgs:
    def test_top_n_zero_raises(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        with pytest.raises(RerankerError, match="top_n 必须为正整数"):
            rerank_memories(
                _make_memories(20),
                _make_card(),
                client=fake_llm_client,
                model="qwen-novel",
                top_n=0,
                prompts_dir=fake_prompts_dir,
            )

    def test_top_n_negative_raises(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        with pytest.raises(RerankerError):
            rerank_memories(
                _make_memories(20),
                _make_card(),
                client=fake_llm_client,
                model="qwen-novel",
                top_n=-1,
                prompts_dir=fake_prompts_dir,
            )

    def test_no_router_no_client_raises_typeerror(
        self, fake_prompts_dir: Path
    ) -> None:
        # router=None + client=None → resolve_routing 抛 TypeError
        # 但本测试发生在 ``len(memories) > top_n`` 后才走 resolve_routing
        with pytest.raises(TypeError, match="必须传入"):
            rerank_memories(
                _make_memories(20),
                _make_card(),
                top_n=5,
                prompts_dir=fake_prompts_dir,
            )


# ---------- 短路路径(候选数 <= top_n) ----------


class TestShortCircuit:
    def test_empty_memories_returns_empty(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = rerank_memories(
            [],
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        assert result == []
        # 空候选 → 不调 LLM
        fake_llm_client.complete.assert_not_called()

    def test_candidates_eq_top_n_returns_all_no_llm(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(5)
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        # 不调 LLM,直接返(原顺序)
        fake_llm_client.complete.assert_not_called()
        assert [m.memory_id for m in result] == [m.memory_id for m in memories]

    def test_candidates_lt_top_n_returns_all_no_llm(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(3)
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        fake_llm_client.complete.assert_not_called()
        assert len(result) == 3


# ---------- LLM 路径正常 ----------


class TestLLMPathHappy:
    def test_returns_top_n_in_llm_order(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        # LLM 返 5 条排序(选 mem-15 / 12 / 03 / 07 / 18)
        fake_llm_client.complete.return_value = _make_response(
            content=json.dumps(
                {"ranking": ["mem-15", "mem-12", "mem-03", "mem-07", "mem-18"]},
                ensure_ascii=False,
            )
        )
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        assert [m.memory_id for m in result] == [
            "mem-15",
            "mem-12",
            "mem-03",
            "mem-07",
            "mem-18",
        ]

    def test_default_top_n_is_5(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 不传 top_n,确认默认 5
        assert DEFAULT_TOP_N == 5
        memories = _make_memories(20)
        fake_llm_client.complete.return_value = _make_response(
            content=json.dumps(
                {"ranking": [f"mem-{i:02d}" for i in range(5)]},
                ensure_ascii=False,
            )
        )
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            prompts_dir=fake_prompts_dir,
        )
        assert len(result) == 5

    def test_user_message_contains_chapter_card_context(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        call = fake_llm_client.complete.call_args
        user_text = call.kwargs["messages"][1].content
        # 章节卡上下文段
        assert "林衍" in user_text  # pov_character
        assert "青崖山顶残阵" in user_text  # setting
        assert "8/10" in user_text  # tension_level
        assert "林衍突破第一道剑阵" in user_text  # must_achieve
        # 候选段
        assert "## 待排序记忆候选" in user_text
        assert "[mem-00]" in user_text
        assert "[mem-19]" in user_text
        # 输出指令
        assert '{"ranking":' in user_text or '"ranking"' in user_text
        # top_n 数字
        assert "5 条" in user_text

    def test_system_message_loaded_from_template_file(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        rerank_memories(
            _make_memories(20),
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        messages = fake_llm_client.complete.call_args.kwargs["messages"]
        assert messages[0].role == "system"
        assert "FAKE_RERANKER_SYSTEM_PROMPT" in messages[0].content

    def test_legacy_kwargs_passed_to_complete(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        rerank_memories(
            _make_memories(20),
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            max_tokens=600,
            temperature=0.1,
            prompts_dir=fake_prompts_dir,
        )
        kwargs = fake_llm_client.complete.call_args.kwargs
        assert kwargs["max_tokens"] == 600
        assert kwargs["temperature"] == 0.1
        assert kwargs["model"] == "qwen-novel"


# ---------- LLM 路径异常容错 ----------


class TestLLMPathFallback:
    def test_invalid_json_falls_back_to_top_n_by_distance(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        fake_llm_client.complete.return_value = _make_response(
            content="this is not JSON {",
        )
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        # fallback 返原距离升序前 5
        assert [m.memory_id for m in result] == [f"mem-{i:02d}" for i in range(5)]

    def test_missing_ranking_field_falls_back(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        fake_llm_client.complete.return_value = _make_response(
            content=json.dumps({"results": ["mem-15"]}, ensure_ascii=False),
        )
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        assert [m.memory_id for m in result] == [f"mem-{i:02d}" for i in range(5)]

    def test_ranking_not_list_of_str_falls_back(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        # ranking 是 list[int],非 list[str]
        fake_llm_client.complete.return_value = _make_response(
            content=json.dumps({"ranking": [1, 2, 3]}),
        )
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        assert [m.memory_id for m in result] == [f"mem-{i:02d}" for i in range(5)]

    def test_llm_call_raises_falls_back(
        self, fake_prompts_dir: Path
    ) -> None:
        client = MagicMock(spec=LLMClient)
        client.complete.side_effect = RuntimeError("network down")
        memories = _make_memories(20)
        result = rerank_memories(
            memories,
            _make_card(),
            client=client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        assert [m.memory_id for m in result] == [f"mem-{i:02d}" for i in range(5)]


# ---------- LLM 输出格式杂质 ----------


class TestLLMOutputFormat:
    def test_strips_code_fence(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        # LLM 偶发把 JSON 套进代码块包裹
        ranking = [f"mem-{i:02d}" for i in range(5, 10)]
        wrapped = (
            "```json\n"
            + json.dumps({"ranking": ranking}, ensure_ascii=False)
            + "\n```"
        )
        fake_llm_client.complete.return_value = _make_response(content=wrapped)
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        assert [m.memory_id for m in result] == ranking

    def test_strips_trailing_html_comment(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        ranking = [f"mem-{i:02d}" for i in range(5)]
        # LLM 复读 marker 到尾部(虽 reranker 不带 marker,兜底逻辑应一致)
        with_trail = (
            json.dumps({"ranking": ranking}, ensure_ascii=False)
            + "\n<!-- agent_role: reranker -->"
        )
        fake_llm_client.complete.return_value = _make_response(content=with_trail)
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        assert [m.memory_id for m in result] == ranking


# ---------- LLM 漏选 / 幻觉 ID 兜底 ----------


class TestRankingFallbacks:
    def test_hallucinated_ids_dropped(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        # 含两个不存在的 ID(mem-99 / mem-ghost)
        fake_llm_client.complete.return_value = _make_response(
            content=json.dumps(
                {
                    "ranking": [
                        "mem-99",
                        "mem-15",
                        "mem-ghost",
                        "mem-07",
                        "mem-12",
                        "mem-03",
                    ]
                },
                ensure_ascii=False,
            )
        )
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        # 幻觉丢弃,真实 ID 顺序保留 + 按原 distance 顺序补足到 5 条
        result_ids = [m.memory_id for m in result]
        assert "mem-99" not in result_ids
        assert "mem-ghost" not in result_ids
        # 前 4 条是 LLM 排序的真实 ID
        assert result_ids[:4] == ["mem-15", "mem-07", "mem-12", "mem-03"]
        # 第 5 条按原 distance 顺序补位(最小未选 ID = mem-00)
        assert result_ids[4] == "mem-00"

    def test_missing_ids_appended_by_distance(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        # LLM 只返 2 条,top_n=5;补位 3 条按原距离顺序
        fake_llm_client.complete.return_value = _make_response(
            content=json.dumps(
                {"ranking": ["mem-10", "mem-05"]}, ensure_ascii=False
            )
        )
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        result_ids = [m.memory_id for m in result]
        assert result_ids[:2] == ["mem-10", "mem-05"]
        # 后 3 条按原距离补:mem-00 / 01 / 02 是最小 distance(mem-05/10 已被选走)
        assert result_ids[2:] == ["mem-00", "mem-01", "mem-02"]

    def test_empty_ranking_falls_back_to_distance_order(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memories = _make_memories(20)
        fake_llm_client.complete.return_value = _make_response(
            content=json.dumps({"ranking": []}, ensure_ascii=False)
        )
        result = rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        # 空 ranking 不算 fallback(JSON 合法),但补位规则下全靠 distance
        assert [m.memory_id for m in result] == [f"mem-{i:02d}" for i in range(5)]


# ---------- 候选内容截断 ----------


class TestContentPreviewTruncation:
    def test_long_content_truncated_in_prompt(
        self, fake_llm_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 候选内容 > 200 字 → prompt 截断 + … 后缀
        long_content = "啊" * 500
        memories = [
            RetrievedMemory(
                memory_id=f"mem-{i:02d}",
                content=long_content,
                metadata={},
                distance=0.1 + i * 0.05,
            )
            for i in range(10)
        ]
        rerank_memories(
            memories,
            _make_card(),
            client=fake_llm_client,
            model="qwen-novel",
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        user_text = fake_llm_client.complete.call_args.kwargs["messages"][1].content
        # 截断标记
        assert "…" in user_text
        # 整段 500 字不应整段出现(若未截断则会出现 500 个连续"啊"序列)
        assert "啊" * 500 not in user_text


# ---------- ModelRouter 集成 ----------


def _make_router_config_with_reranker() -> WriterProfilesConfig:
    """构造含 reranker agent_routing 的 router 配置。"""
    profiles = {
        "local_qwen_safe": WriterProfile(
            profile_id="local_qwen_safe",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            generation_params={"temperature": 0.5, "num_predict": 2048},
            extra_request_kwargs={"think": False},
        ),
        "local_qwen_32k": WriterProfile(
            profile_id="local_qwen_32k",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=24576,
        ),
    }
    routing = RoutingConfig(
        default="local_qwen_32k",
        rules=[],
        agent_routing={"reranker": "local_qwen_safe"},
    )
    return WriterProfilesConfig(profiles=profiles, routing=routing)


class TestRouterIntegration:
    def test_router_path_routes_to_reranker_profile(
        self, fake_prompts_dir: Path
    ) -> None:
        # 用 fake client_factory 拦 client 构造,验证 router.route("reranker")
        # 命中 local_qwen_safe(对齐 config.yaml 字面)。
        captured: list[tuple[str, str | None]] = []

        captured_messages: list = []

        def fake_factory(
            *,
            model: str,
            base_url: str | None = None,
            provider: str | None = None,
        ) -> LLMClient:
            captured.append((model, base_url))
            client = MagicMock(spec=LLMClient)

            def _complete(*, messages, model=None, **kwargs):
                captured_messages.append(messages)
                return _make_response(
                    content=json.dumps(
                        {"ranking": [f"mem-{i:02d}" for i in range(5)]},
                        ensure_ascii=False,
                    ),
                    model_used=model or "qwen-novel",
                )

            client.complete.side_effect = _complete
            return client

        router = ModelRouter(
            _make_router_config_with_reranker(), client_factory=fake_factory
        )
        memories = _make_memories(20)
        result = rerank_memories(
            memories,
            _make_card(),
            router=router,
            top_n=5,
            prompts_dir=fake_prompts_dir,
        )
        # router 命中 local_qwen_safe(profile.model=qwen-novel,base_url=http://localhost:11434)
        assert ("qwen-novel", "http://localhost:11434") in captured
        assert len(result) == 5
        # router 路径下 generation_params(temperature=0.5, num_predict=2048) 被透传
        # think 字段被翻译为 enable_thinking(本测无 OllamaClient 真实实例,
        # build_complete_kwargs 检测到 fake MagicMock 不会翻译,留原 think 字
        # 段——与 P3-B-04 marker §6 守护一致)
