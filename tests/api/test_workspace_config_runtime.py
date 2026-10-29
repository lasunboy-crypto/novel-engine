"""P7-B-05 caller 链工作区覆盖运行时单测。

验证 P6-B-13 / B-14 / B-15 的工作区级 yaml 覆盖在 caller 链运行时即时生效:

- **retriever**:``retrieve_memories_for_chapter(card, vector_store, workspace_id=...)``
  在 ``top_k=None`` 时按工作区 retrieval.top_k 解析(P6-B-14 字段)
- **reviewer**:``review_chapter(chapter_card, ..., workspace_id=...)`` 在工作区
  reviewer.pass_threshold / revise_threshold 给定时覆盖 ChapterCard 单章字段
  (P6-B-13 字段)
- **cost_guard**:``get_workspace_cost_guard_config(workspace_id, default=...)``
  工作区 project.yaml 含 ``cost_guard:`` 段时按工作区配置走;无段则回退 default
  (注入单例),保持 P1-P6 行为字面不变(P6-B-15 字段)

3 caller 各 1-2 测覆盖工作区覆盖生效路径 + 无覆盖回退路径,共 6 测。

fixture 模式对齐 ``tests/services/test_config_service.py``(tmp_path engine yaml
+ workspaces_root + monkeypatch settings.data_dir 让 caller 内部走临时路径)。
DATA_DIR 不需要 — 全 in-memory 工作区 yaml + mock 依赖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.config import settings
from src.core.agents.reviewer import review_chapter
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage  # noqa: F401
from src.core.memory.retriever import (
    DEFAULT_TOP_K,
    retrieve_memories_for_chapter,
)
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.services.cost_guard import (
    CostGuardConfig,
    get_workspace_cost_guard_config,
    reset_workspace_cost_guard_config_cache,
)

# ---- 共享 fixture(tmp_path + monkeypatch settings)-------------------------


_ENGINE_YAML = """\
writer_profiles:
  cloud_quality:
    provider: claude
    model: claude-sonnet-4-6
    context_window: 200000
    generation_params: {temperature: 0.85}
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015

writer_routing:
  default: cloud_quality

reviewer:
  pass_threshold: 7.5
  revise_threshold: 6.5
  escalation_lower_bound: 6.0
  escalation_upper_bound: 8.0

retrieval:
  top_k: 20
  min_similarity: 0.0

cost_guard:
  daily_usd_limit: 20.0
  monthly_usd_limit: 300.0
  per_workspace_daily_limit: 10.0
  action_on_exceed: pause
  notify_via: [log, websocket]
"""


@pytest.fixture
def engine_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时引擎默认 ``config.yaml`` + monkeypatch ``settings.config_yaml_path``。"""
    p = tmp_path / "config.yaml"
    p.write_text(_ENGINE_YAML, encoding="utf-8")
    monkeypatch.setattr(settings, "config_yaml_path", p)
    return p


@pytest.fixture
def workspaces_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 ``workspaces/`` 根 + monkeypatch ``settings.data_dir``。

    ``ConfigService()`` 默认从 ``settings.data_dir`` 取根,caller 链内的
    ``get_workspace_cost_guard_config`` / ``ConfigService().load_*`` 都走此路径。
    """
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(settings, "data_dir", root)
    return root


def _write_workspace_yaml(
    workspaces_root: Path, workspace_id: str, content: str
) -> Path:
    """在 ``workspaces_root/<workspace_id>/project.yaml`` 写 yaml 内容。"""
    ws_root = workspaces_root / workspace_id
    ws_root.mkdir(exist_ok=True)
    p = ws_root / "project.yaml"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _reset_cost_guard_cache() -> Any:
    """每个用例前后清 cost_guard workspace lazy 缓存,防止用例间串扰。"""
    reset_workspace_cost_guard_config_cache()
    yield
    reset_workspace_cost_guard_config_cache()


# ---- ChapterCard / VectorStore mock 辅助 -----------------------------------


def _make_card(**overrides: Any) -> ChapterCard:
    payload: dict[str, Any] = dict(
        chapter_id="ch-0001",
        workspace_id="ws-runtime",
        volume_id="v01",
        chapter_number=1,
        title="第一章",
        tension_level=4,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["首章必达"],
        must_unresolved=["首章悬念"],
        forbidden=[],
        pov_character="林衍",
        setting="青崖山",
        continuity_context=ContinuityContext(
            prev_chapter_tail="(首章)",
            unfinished_actions=[],
            time_gap="立即",
            scene_continuation="same",
            pov_change=False,
            pov_from=None,
            pov_to="林衍",
        ),
        next_chapter_seed="次回伏笔",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=[],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _make_mock_vector_store_capture_top_k() -> MagicMock:
    """构造 mock ``VectorStore``,``query()`` 返空命中并记录 ``top_k`` 入参。"""
    vs = MagicMock(spec=VectorStore)
    vs.query.return_value = []
    return vs


# =============================================================================
# 1. retriever caller 链(P6-B-14 retrieval.top_k 字段)
# =============================================================================


class TestRetrieverWorkspaceTopK:
    """``retrieve_memories_for_chapter(card, ..., top_k=None, workspace_id=...)``
    在 ``top_k=None`` 时按工作区 retrieval.top_k 解析。"""

    def test_workspace_override_top_k_applied(
        self, engine_yaml: Path, workspaces_root: Path
    ) -> None:
        """工作区 project.yaml ``retrieval.top_k: 5`` → retriever 用 5 调 vector_store。"""
        _write_workspace_yaml(
            workspaces_root,
            "ws-runtime",
            "retrieval:\n  top_k: 5\n",
        )
        vs = _make_mock_vector_store_capture_top_k()
        retrieve_memories_for_chapter(
            _make_card(),
            vector_store=vs,
            top_k=None,
            workspace_id="ws-runtime",
        )
        called_top_k = vs.query.call_args.kwargs["top_k"]
        assert called_top_k == 5, (
            f"工作区 top_k=5 未生效,实际调 vector_store 用 {called_top_k}"
        )

    def test_no_workspace_override_falls_back_to_default(
        self, engine_yaml: Path, workspaces_root: Path
    ) -> None:
        """无 project.yaml + ``top_k=None`` + ``workspace_id`` 给定 →
        走 ConfigService.load_retrieval_config 返引擎默认值 20。
        """
        vs = _make_mock_vector_store_capture_top_k()
        retrieve_memories_for_chapter(
            _make_card(),
            vector_store=vs,
            top_k=None,
            workspace_id="ws-no-override",
        )
        called_top_k = vs.query.call_args.kwargs["top_k"]
        assert called_top_k == DEFAULT_TOP_K == 20


# =============================================================================
# 2. reviewer caller 链(P6-B-13 reviewer.pass_threshold / revise_threshold 字段)
# =============================================================================


class TestReviewerWorkspaceThresholds:
    """``review_chapter(..., workspace_id=...)`` 工作区阈值覆盖 ChapterCard 字段。"""

    @staticmethod
    def _fake_reviewer_llm(score: float = 7.0) -> MagicMock:
        """构造 fake LLM,返一份固定分数的 reviewer JSON 输出。

        各分项分 = ``score``,综合分按 PROJECT_SPEC §4.4 权重等于 ``score``
        (各项权重总和 1.0,所有分项相等时综合分 = 分项分)。
        """
        import json

        payload = {
            "score_plot_progression": score,
            "score_tension_match": score,
            "score_prose_quality": score,
            "score_consistency": score,
            "score_hook_management": score,
            "score_voice_consistency": score,
            "hard_constraints_passed": True,
            "failed_constraints": [],
            "issues": [],
            "suggestions": [],
        }
        fake = MagicMock(spec=LLMClient)
        fake.complete.return_value = LLMResponse(
            content=json.dumps(payload),
            model_used="mock-model",
            usage=TokenUsage(input_tokens=100, output_tokens=200),
            finish_reason="end_turn",
        )
        return fake

    @pytest.fixture
    def fake_prompts_dir(self, tmp_path: Path) -> Path:
        """fake reviewer.md 模板。"""
        d = tmp_path / "prompts_p7b05"
        d.mkdir()
        (d / "reviewer.md").write_text("FAKE_REVIEWER_SYSTEM", encoding="utf-8")
        return d

    def test_workspace_higher_pass_threshold_demotes_to_revise(
        self,
        engine_yaml: Path,
        workspaces_root: Path,
        fake_prompts_dir: Path,
    ) -> None:
        """ChapterCard ``review_threshold=7.5`` + score=8.0 → 默认 pass;
        工作区 ``pass_threshold=8.5`` + revise_threshold=7.0 → 同分降级到 revise。
        """
        _write_workspace_yaml(
            workspaces_root,
            "ws-runtime",
            "reviewer:\n  pass_threshold: 8.5\n  revise_threshold: 7.0\n",
        )
        result = review_chapter(
            chapter_card=_make_card(),  # ChapterCard 默认 review=7.5 / revise=6.5
            chapter_text="略",
            chapter_id="ch-0001",
            version_reviewed=1,
            client=self._fake_reviewer_llm(score=8.0),
            model="mock-model",
            prompts_dir=fake_prompts_dir,
            workspace_id="ws-runtime",
        )
        # 工作区 pass=8.5 > score=8.0 → 不 pass;revise=7.0 <= 8.0 → revise
        assert result.verdict == "revise"
        assert result.score_overall == pytest.approx(8.0)

    def test_no_workspace_override_uses_chapter_card_thresholds(
        self,
        engine_yaml: Path,
        workspaces_root: Path,
        fake_prompts_dir: Path,
    ) -> None:
        """工作区无 project.yaml → 退化到 ChapterCard 字段(P3-P6 既有行为)。"""
        result = review_chapter(
            chapter_card=_make_card(),  # review=7.5 / revise=6.5
            chapter_text="略",
            chapter_id="ch-0001",
            version_reviewed=1,
            client=self._fake_reviewer_llm(score=8.0),
            model="mock-model",
            prompts_dir=fake_prompts_dir,
            workspace_id="ws-no-override",
        )
        # ConfigService 加载工作区无 yaml 时返引擎默认 reviewer 段 = ChapterCard
        # 同语义阈值(pass=7.5),score=8.0 >= 7.5 → pass。
        assert result.verdict == "pass"


# =============================================================================
# 3. cost_guard caller 链(P6-B-15 cost_guard.* 字段)
# =============================================================================


class TestCostGuardWorkspaceConfig:
    """``get_workspace_cost_guard_config(workspace_id, default=...)`` 路径。"""

    def test_workspace_override_yields_stricter_limit(
        self, engine_yaml: Path, workspaces_root: Path
    ) -> None:
        """工作区 project.yaml 含 ``cost_guard.per_workspace_daily_limit: 1.0`` →
        返合并后配置(覆盖 default 10.0)。
        """
        _write_workspace_yaml(
            workspaces_root,
            "ws-runtime",
            "cost_guard:\n  per_workspace_daily_limit: 1.0\n",
        )
        default = CostGuardConfig(per_workspace_daily_limit=10.0)
        cfg = get_workspace_cost_guard_config("ws-runtime", default=default)
        assert cfg.per_workspace_daily_limit == 1.0  # 工作区覆盖生效
        # 其他字段保留引擎默认(字段级合并 P6-B-15 语义)
        assert cfg.daily_usd_limit == 20.0

    def test_no_workspace_override_returns_injected_default(
        self, engine_yaml: Path, workspaces_root: Path
    ) -> None:
        """工作区无 cost_guard 段 → 返 default(注入单例,保持 P1-P6 行为不变)。"""
        # 工作区 yaml 含其他段但无 cost_guard 段
        _write_workspace_yaml(
            workspaces_root,
            "ws-runtime",
            "reviewer:\n  pass_threshold: 8.0\n",
        )
        injected_default = CostGuardConfig(per_workspace_daily_limit=99.9)
        cfg = get_workspace_cost_guard_config(
            "ws-runtime", default=injected_default
        )
        assert cfg.per_workspace_daily_limit == 99.9  # 走 default,无工作区覆盖
        assert cfg is injected_default  # 同一实例,不落缓存
