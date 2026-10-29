"""ChapterService 测试(P1-B-15)。

测试组织:
  - TestWriteFullPipeline:plan 验收主路径(write → review → revise → review
    → pass → 落 final.md)
  - TestWrite:write 入口的预处理边界(工作区 / 章节卡缺失、chapter_id 不一致)
  - TestRewrite:rewrite 起始状态 + caller prior count 不再可信
  - TestRevise:revise 不走 Orchestrator,产出新版本不影响 final
  - TestStatusFsFallback:7 档 fs fallback 路径(无 session_factory)
  - TestStatusDbBacked:7 档 DB-priority 路径(注入 session_factory,P2-B-17)
  - TestSwitchVersion:版本切换 = mark_final
  - TestChapterCardIO:章节卡读写 round-trip + 错误翻译
  - TestWorkspaceValidation:WorkspaceService.info 失败时翻译为 WorkspaceServiceError

mock LLMClient(``MagicMock(spec=LLMClient)``)+ ``side_effect`` 列表;
Orchestrator / Reviser agent / 状态机 / ChapterVersioning / FileMemory 全真跑。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import settings
from src.core.agents import _prompt as prompt_module
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.core_state import (
    BookClockInterval,
    build_book_clock_constraint,
    make_core_state_projection,
)
from src.core.models.review import ReviewIssue
from src.core.rules.prose_revision_contract import (
    ProseRevisionContractError,
    ProseRevisionNoProgressError,
)
from src.core.state_machine.states import ChapterState
from src.services.chapter_service import (
    ChapterService,
    ChapterServiceError,
    ChapterStatusInfo,
)
from src.services.chapter_rewind_service import REWIND_REWRITE_REQUIRED_REASON
from src.services.workspace_service import (
    WorkspaceService,
    WorkspaceServiceError,
)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="他握紧剑柄,目光不离对面那道身影。",
        unfinished_actions=[],  # P4-B-07:衔接桥默认通过
        time_gap="立即续接",
        scene_continuation="switch",  # P4-B-07:跳过 scene_continuation_location
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(chapter_id: str = "ch-0002", **overrides) -> ChapterCard:
    payload: dict = dict(
        chapter_id=chapter_id,
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章·破阵",
        tension_level=8,
        # P4-B-06:validator 默认放宽,本文件测 chapter_service,validator 不参与。
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=3,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_make_continuity(),
        next_chapter_seed="苏离消失前留下的玉佩在颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["林衍"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _writer_response(content: str = "正文(略)") -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=1000, output_tokens=2000),
        finish_reason="end_turn",
    )


def _reviewer_response(
    *,
    score: float = 8.0,
    hard_constraints_passed: bool = True,
    issues: list[dict] | None = None,
) -> LLMResponse:
    payload = {
        "score_plot_progression": score,
        "score_tension_match": score,
        "score_prose_quality": score,
        "score_consistency": score,
        "score_hook_management": score,
        "score_voice_consistency": score,
        "hard_constraints_passed": hard_constraints_passed,
        "failed_constraints": [] if hard_constraints_passed else ["违反 forbidden"],
        "issues": issues
        if issues is not None
        else [
            {
                "paragraph_anchor": "他握紧剑柄,目光不离对面那道身影",
                "issue_type": "pacing",
                "description": "节奏过缓",
                "suggestion": "压缩为 2-3 句",
            }
        ],
        "suggestions": ["下章压缩句长"],
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=1500, output_tokens=400),
        finish_reason="end_turn",
    )


def _summary_response(evidence_source_id: str = "E0001") -> LLMResponse:
    """返 R4 严格小账本所需的最小 source ID summary payload。"""
    payload = {
        "summary_text": "林衍在测试场景完成本章动作。",
        "pov_character": "林衍",
        "pov_evidence_source_id": evidence_source_id,
        "scene_setting": "测试场景",
        "scene_setting_evidence_source_id": evidence_source_id,
        "key_events": [],
        "character_interactions": [],
        "unfinished_actions": [],
        "character_states": [],
        "established_facts": [],
        "revealed_mechanics": [],
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=100, output_tokens=100),
        finish_reason="end_turn",
    )


def _reviser_response(content: str = "修订后正文(略)") -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=2500, output_tokens=2200),
        finish_reason="end_turn",
    )


def _contract_sequence(*responses):
    """让局部修订响应读取当次动态 source/plan 身份。"""
    iterator = iter(responses)

    def _next(*args, **kwargs):
        del args
        response = next(iterator)
        if callable(response):
            return response(kwargs["messages"])
        return response

    return _next


def _localized_reviser_response(messages, *, replacement: str) -> LLMResponse:
    user_text = messages[1].content
    identity_tail = user_text.split("### 冻结身份与允许范围", 1)[1]
    identity_json = identity_tail.split("```json\n", 1)[1].split("\n```", 1)[0]
    identity = json.loads(identity_json)
    target = identity["targets"][0]
    payload = {
        "contract_version": "1",
        "mode": "localized",
        "source_version_id": identity["source_version_id"],
        "source_sha256": identity["source_sha256"],
        "plan_hash": identity["plan_hash"],
        "patches": [
            {
                "target_ids": [target["target_id"]],
                "anchor_id": target["allowed_anchor_ids"][0],
                "operation": "replace",
                "replacement": replacement,
            }
        ],
    }
    return _reviser_response(json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _valid_character_admission_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本文件不测角色缺失；正常路径显式提供合法林衍权威记录。"""
    character = MagicMock()
    character.character_id = "林衍"
    character.name = "林衍"
    character.current_state = {}
    character.voice_profile.forbidden_phrases = []
    character.identity_facets = []

    def _list_characters(*_args: object) -> list[MagicMock]:
        return [character]

    monkeypatch.setattr(
        "src.services.chapter_service.list_characters_by_workspace",
        _list_characters,
    )
    monkeypatch.setattr(
        "src.core.orchestrator.chapter_orchestrator.list_characters_by_workspace",
        _list_characters,
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """tmp_path 下建写作与 R4 ledger 所需的 fake 模板。"""
    prompts_dir = tmp_path / "prompts_root"
    prompts_dir.mkdir()
    (prompts_dir / "writer.md").write_text("FAKE_WRITER", encoding="utf-8")
    (prompts_dir / "reviewer.md").write_text("FAKE_REVIEWER", encoding="utf-8")
    (prompts_dir / "reviser.md").write_text("FAKE_REVISER", encoding="utf-8")
    (prompts_dir / "chapter_summary_extractor_v2.md").write_text(
        "FAKE_CHAPTER_SUMMARY_EXTRACTOR", encoding="utf-8"
    )
    (prompts_dir / "chapter_summary_extractor_v3.md").write_text(
        "FAKE_CHAPTER_SUMMARY_EXTRACTOR_SOURCE_REFS", encoding="utf-8"
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _patch_settings_prompts_dir(
    monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path
) -> None:
    """三个 agent 默认从 ``settings.prompts_dir`` 取模板,测试统一注入 fake。"""
    monkeypatch.setattr(settings, "prompts_dir", fake_prompts_dir)


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """三个 agent 共享 ``load_prompt_template`` LRU 缓存,每用例清一次。"""
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    """每个测试给一个隔离的 ``workspaces/`` + ``archives/``。"""
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture
def mock_client() -> MagicMock:
    """mock LLM 客户端;每用例自行设置 ``side_effect``。"""
    return MagicMock(spec=LLMClient)


@pytest.fixture
def chapter_service(
    workspace_service: WorkspaceService, mock_client: MagicMock
) -> ChapterService:
    return ChapterService(
        workspace_service=workspace_service,
        client=mock_client,
        model="claude-sonnet-4-6",
    )


@pytest.fixture
def prepared_workspace(
    chapter_service: ChapterService,
    workspace_service: WorkspaceService,
) -> str:
    """创建 ws-demo 工作区 + 落一份 ch-0002 章节卡;返回 workspace_id。"""
    workspace_service.create(
        "ws-demo",
        display_name="科幻测试本",
        genre="scifi",
        premise="人类在月球建殖民地",
        total_chapters=120,
    )
    chapter_service.save_chapter_card("ws-demo", _make_card())
    return "ws-demo"


# ---------------------------------------------------------------------------
# 1. plan 验收主路径
# ---------------------------------------------------------------------------


class TestWriteFullPipeline:
    """plan 验收用例:write → review → revise → review → pass → 落 final.md。"""

    def test_full_pipeline_write_review_revise_review_pass(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
    ) -> None:
        # write → revise → pass:LLM 调用顺序为 Writer / Reviewer / Reviser / Reviewer
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content="他握紧剑柄,目光不离对面那道身影"),
            _reviewer_response(score=7.0),  # 综合分 7.0 ∈ [6.5, 7.5) → revise
            lambda messages: _localized_reviser_response(
                messages, replacement="修订稿"
            ),
            _reviewer_response(score=8.0),  # pass
        )

        result = chapter_service.write(prepared_workspace, "ch-0002")

        assert result.final_state == ChapterState.APPROVED
        assert result.final_version_id == 2
        assert result.escalation_reason is None
        assert result.revise_count == 1
        assert result.rewrite_count == 0
        assert [v.creation_type for v in result.versions] == ["draft", "revise"]

        # final.md 真落盘且内容 = v2 修订稿
        assert result.final_path is not None
        assert result.final_path == (
            tmp_path
            / "workspaces"
            / "ws-demo"
            / "data"
            / "chapters"
            / "ch-0002"
            / "final.md"
        )
        assert result.final_path.read_text(encoding="utf-8") == "修订稿"

    def test_automatic_localized_reviser_noop_stops_before_version_and_rereview(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
    ) -> None:
        source = "他握紧剑柄,目光不离对面那道身影"
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=source),
            _reviewer_response(score=7.0),
            lambda messages: _localized_reviser_response(
                messages,
                replacement=source,
            ),
        )

        with pytest.raises(ProseRevisionNoProgressError, match="实质变化"):
            chapter_service.write(prepared_workspace, "ch-0002")

        chapter_dir = (
            tmp_path / "workspaces" / "ws-demo" / "data" / "chapters" / "ch-0002"
        )
        assert (chapter_dir / "v1.md").exists()
        assert not (chapter_dir / "v2.md").exists()
        assert mock_client.complete.call_count == 3


# ---------------------------------------------------------------------------
# 2. write 入口边界
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_validates_workspace_first(
        self, chapter_service: ChapterService, mock_client: MagicMock
    ) -> None:
        """工作区不存在 → WorkspaceServiceError(本质是工作区不合法)。"""
        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            chapter_service.write("ghost-ws", "ch-0001")
        # LLM 不应被调用
        mock_client.complete.assert_not_called()

    def test_write_missing_chapter_card_raises(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """工作区合法但章节卡缺失 → ChapterServiceError。"""
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        with pytest.raises(ChapterServiceError, match="章节卡不存在"):
            chapter_service.write("ws-demo", "ch-missing")
        mock_client.complete.assert_not_called()

    def test_write_chapter_id_mismatch_raises(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
    ) -> None:
        """章节卡内 chapter_id 与传入不一致 → ChapterServiceError。

        构造手段:save 一张 ch-0002 卡,但显式 ``copy`` 一份到 ch-0003.json,
        模拟维护人手工放入的"文件名 != chapter_id"的卡。
        """
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        card = _make_card(chapter_id="ch-0002")
        chapter_service.save_chapter_card("ws-demo", card)
        # 把同一份卡再写到 ch-0003.json(模拟文件名错位)
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(
            "ws-demo", workspaces_root=workspace_service.workspaces_root
        )
        (paths.cards_dir / "ch-0003.json").write_text(
            card.model_dump_json(indent=2), encoding="utf-8"
        )

        with pytest.raises(ChapterServiceError, match="不一致"):
            chapter_service.write("ws-demo", "ch-0003")

    @pytest.mark.parametrize("operation", ["write", "rewrite"])
    def test_common_orchestrator_revalidates_dramatic_source_before_provider(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
    ) -> None:
        """正常写作与恢复/重写共用同一个 Writer 前 typed source 闸。"""
        preflight = MagicMock(
            side_effect=ChapterServiceError("A1 typed source drift")
        )
        monkeypatch.setattr(
            chapter_service,
            "_validate_card_dramatic_source_preflight",
            preflight,
        )

        with pytest.raises(ChapterServiceError, match="typed source drift"):
            getattr(chapter_service, operation)(prepared_workspace, "ch-0002")

        preflight.assert_called_once()
        mock_client.complete.assert_not_called()


# ---------------------------------------------------------------------------
# 3. rewrite
# ---------------------------------------------------------------------------


class TestRewrite:
    def _checkpoint_service(
        self,
        *,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        workspace_id: str,
        write_v1_file: bool,
        constraint_results: list[dict] | None,
        failed_constraints: list[str] | str | None = None,
        constraint_violations: list[dict] | str | None = None,
    ):
        """构造一个 DB/正文 checkpoint 可控的产品 Rewrite 服务。"""
        from datetime import UTC, datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from src.core.chapter_versioning import ChapterVersioning
        from src.core.db import Base
        from src.core.db.models import (
            ChapterORM,
            ChapterVersionORM,
            ReviewResultORM,
        )
        from src.core.rules.style_fingerprint import compute_style_fingerprint
        from src.core.workspace.workspace_paths import WorkspacePaths
        from src.services.asset_import import ensure_workspace_orm

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)

        def _session_factory(requested_workspace_id: str) -> Session:
            assert requested_workspace_id == workspace_id
            return Session(engine)

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_session_factory,
        )
        card = _make_card("ch-0001", chapter_number=1)
        service.save_chapter_card(workspace_id, card)

        content = "产品 checkpoint 正文" * 80
        now = datetime.now(UTC)
        if write_v1_file:
            version = ChapterVersioning(paths).write_version(
                card.chapter_id,
                content,
                creation_type="draft",
                model_used="mock",
            )
            created_at = version.created_at
            word_count = version.word_count
            style_fingerprint = version.style_fingerprint
        else:
            created_at = now
            word_count = len(content)
            style_fingerprint = compute_style_fingerprint(content)

        with Session(engine) as session:
            ensure_workspace_orm(session, workspace_service.info(workspace_id))
            session.add(
                ChapterORM(
                    chapter_id=card.chapter_id,
                    workspace_id=workspace_id,
                    current_version=1,
                    status="human_intervention",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=None,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
            session.add(
                ChapterVersionORM(
                    chapter_id=card.chapter_id,
                    version_id=1,
                    content=content,
                    word_count=word_count,
                    created_at=created_at,
                    creation_type="draft",
                    model_used="mock",
                    review_score=None,
                    notes="",
                    style_fingerprint=style_fingerprint,
                )
            )
            if constraint_results is not None:
                session.add(
                    ReviewResultORM(
                        review_id="rv-checkpoint",
                        chapter_id=card.chapter_id,
                        version_reviewed=1,
                        reviewed_at=now,
                        reviewer_model="qwen3.7-max",
                        score_plot_progression=8.0,
                        score_tension_match=8.0,
                        score_prose_quality=8.0,
                        score_consistency=8.0,
                        score_hook_management=8.0,
                        score_voice_consistency=8.0,
                        score_overall=8.0,
                        hard_constraints_passed=False,
                        failed_constraints=(
                            ["style_drift"]
                            if failed_constraints is None
                            else failed_constraints  # type: ignore[arg-type]
                        ),
                        constraint_violations=(
                            []
                            if constraint_violations is None
                            else constraint_violations  # type: ignore[arg-type]
                        ),
                        constraint_results=constraint_results,
                        hard_constraint_regressions=[],
                        suggestions=[],
                        verdict="rewrite",
                    )
                )
            session.commit()
        return service, engine

    def test_rewrite_starts_from_rewriting_state(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """rewrite 直接调 Writer 写新版本(creation_type='rewrite'),不经 DRAFTING。"""
        mock_client.complete.side_effect = [
            _writer_response(content="重写稿 v1"),
            _reviewer_response(score=8.0),  # pass
        ]
        result = chapter_service.rewrite(prepared_workspace, "ch-0002")

        assert result.final_state == ChapterState.APPROVED
        assert result.rewrite_count == 1  # REWRITING 入口本身消费 1 次 content rewrite
        assert [v.creation_type for v in result.versions] == ["rewrite"]
        assert result.final_path is not None
        assert result.final_path.read_text(encoding="utf-8") == "重写稿 v1"

    def test_authoritative_budget_excludes_local_paragraph_reflow(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """机械插空行保留 rewrite 版本语义，但不消耗 content rewrite 名额。"""
        from datetime import UTC, datetime

        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterVersionORM

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=[],
        )
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.add_all(
                [
                    ChapterVersionORM(
                        chapter_id="ch-0001",
                        version_id=2,
                        content="模型全文重写",
                        word_count=6,
                        created_at=now,
                        creation_type="rewrite",
                        model_used="qwen3.7-max",
                        review_score=None,
                        notes="",
                        style_fingerprint=None,
                    ),
                    ChapterVersionORM(
                        chapter_id="ch-0001",
                        version_id=3,
                        content="模型全文重写。\n\n第二段。",
                        word_count=13,
                        created_at=now,
                        creation_type="rewrite",
                        model_used="local:paragraph_reflow",
                        review_score=None,
                        notes="",
                        style_fingerprint=None,
                    ),
                ]
            )
            session.commit()

        budget = service._read_authoritative_budget(  # noqa: SLF001
            prepared_workspace, "ch-0001"
        )
        assert budget.current_version == 3
        assert budget.rewrite_count == 1

    def test_write_auto_recovers_legacy_duplicate_global_rewrite(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """旧 v3==v2 不占有效预算；Run 的普通 write 直接从 v3 自动重写。"""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy.orm import Session

        from src.core.chapter_versioning import ChapterVersioning
        from src.core.db.models import (
            ChapterORM,
            ChapterVersionORM,
            ReviewResultORM,
        )
        from src.core.workspace.workspace_paths import WorkspacePaths

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=[],
        )
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        versioning = ChapterVersioning(paths)
        changed = "第一次 GLOBAL 已经改过正文，但仍未修好失败项。"
        v2 = versioning.write_version(
            "ch-0001",
            changed,
            creation_type="rewrite",
            model_used="qwen3.7-max",
        )
        v3 = versioning.write_version(
            "ch-0001",
            changed + "\n\n",
            creation_type="rewrite",
            model_used="qwen3.7-max",
        )
        reviewed_at = datetime.now(UTC) + timedelta(seconds=1)
        with Session(engine) as session:
            for version in (v2, v3):
                session.add(
                    ChapterVersionORM(
                        chapter_id="ch-0001",
                        version_id=version.version_id,
                        content=version.content,
                        word_count=version.word_count,
                        created_at=version.created_at,
                        creation_type=version.creation_type,
                        model_used=version.model_used,
                        review_score=None,
                        notes="",
                        style_fingerprint=version.style_fingerprint,
                    )
                )
            session.add(
                ReviewResultORM(
                    review_id="rv-v3-no-progress",
                    chapter_id="ch-0001",
                    version_reviewed=3,
                    reviewed_at=reviewed_at,
                    reviewer_model="qwen3.7-max",
                    score_plot_progression=8.0,
                    score_tension_match=8.0,
                    score_prose_quality=8.0,
                    score_consistency=8.0,
                    score_hook_management=8.0,
                    score_voice_consistency=8.0,
                    score_overall=8.0,
                    hard_constraints_passed=False,
                    failed_constraints=["word_count"],
                    constraint_violations=[],
                    constraint_results=[],
                    hard_constraint_regressions=[],
                    suggestions=[],
                    verdict="rewrite",
                )
            )
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            chapter.current_version = 3
            chapter.status = "human_intervention"
            chapter.rewrite_count = 2  # 修复前旧投影，保留现场
            chapter.escalation_details = {
                "reason": "repeated_hard_constraint_no_progress",
                "constraint_ids": ["a" * 64],
                "plan_hash": "b" * 64,
                "version_reviewed": 3,
            }
            session.commit()

        budget_before = service._read_authoritative_budget(  # noqa: SLF001
            prepared_workspace,
            "ch-0001",
        )
        assert budget_before.current_version == 3
        assert budget_before.rewrite_count == 1
        mock_client.complete.side_effect = [
            _writer_response(content="自动恢复后真正修复的 v4 正文。"),
            _reviewer_response(score=8.0),
            _summary_response(),
        ]

        result = service.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        assert result.final_version_id == 4
        assert result.rewrite_count == 2
        assert [version.creation_type for version in result.versions] == ["rewrite"]
        assert mock_client.complete.call_count == 3
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert chapter.status == "approved"
            assert chapter.current_version == 4
            assert chapter.rewrite_count == 2
            assert session.query(ChapterVersionORM).count() == 4

    def test_write_after_rewind_ignores_old_pass_and_creates_rewrite(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """rewind 重试须撤销旧卡 PASS/HARD 身份并按当前卡生成新正文。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import (
            ChapterORM,
            ChapterVersionORM,
            ReviewResultORM,
        )

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=[],
        )
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            review = session.get(ReviewResultORM, "rv-checkpoint")
            assert chapter is not None
            assert review is not None
            # 模拟第一次 :write 已把状态投影到 rewriting，却在 Provider 前因
            # 旧卡 constraint_id 与当前卡计划不一致而失败的可恢复现场。
            chapter.status = "rewriting"
            chapter.escalation_details = {
                "reason": REWIND_REWRITE_REQUIRED_REASON,
                "source_version_id": 1,
                "source_status": "approved",
                "marked_at": "2026-08-17T00:00:00+00:00",
            }
            review.hard_constraints_passed = True
            review.failed_constraints = []
            review.constraint_results = [
                {
                    "constraint_id": "a" * 64,
                    "domain": "forbidden",
                    "source_path": "chapter_cards/ch-0001.forbidden",
                    "source_hash": "b" * 64,
                    "rule_text": "旧卡禁止项，当前卡已重新生成",
                    "status": "pass",
                    "details": "旧审稿证据",
                }
            ]
            review.verdict = "pass"
            session.commit()

        mock_client.complete.side_effect = [
            _writer_response(content="rewind 后真正生成的新 v2 正文。"),
            _reviewer_response(score=8.0),
            _summary_response(),
        ]

        result = service.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        assert result.final_version_id == 2
        assert result.rewrite_count == 1
        assert [version.creation_type for version in result.versions] == ["rewrite"]
        assert mock_client.complete.call_count == 3
        writer_messages = mock_client.complete.call_args_list[0].kwargs["messages"]
        writer_prompt = "\n".join(message.content for message in writer_messages)
        assert "product_rewind_contract_rebase" in writer_prompt
        assert "旧卡禁止项，当前卡已重新生成" not in writer_prompt
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert chapter.status == "approved"
            assert chapter.current_version == 2
            assert chapter.rewrite_count == 1
            assert chapter.escalation_details is None
            assert session.query(ChapterVersionORM).count() == 2

    def test_explicit_rewrite_does_not_discard_stale_review_without_rewind_marker(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """只有精确 rewind marker 可 rebase；普通 Rewrite 仍拒绝计划外旧 ID。"""
        from src.core.rules.prose_hard_contract import (
            ExecutableConstraintPlanError,
        )

        service, _engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=[
                {
                    "constraint_id": "a" * 64,
                    "domain": "forbidden",
                    "source_path": "chapter_cards/ch-0001.forbidden",
                    "source_hash": "b" * 64,
                    "rule_text": "没有 rewind marker 的旧卡禁止项",
                    "status": "pass",
                    "details": "旧审稿证据",
                }
            ],
        )

        with pytest.raises(
            ExecutableConstraintPlanError,
            match="未在计划中声明",
        ):
            service.rewrite(prepared_workspace, "ch-0001")

        mock_client.complete.assert_not_called()

    def test_rewind_rewrite_intent_expires_after_new_version(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """Writer 已落 v2 后，旧 v1 marker 不得再触发第二次 Writer。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterORM

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=[],
        )
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            chapter.status = "drafting"
            chapter.current_version = 2
            chapter.escalation_details = {
                "reason": REWIND_REWRITE_REQUIRED_REASON,
                "source_version_id": 1,
            }
            session.commit()

        assert (
            service._rewind_requires_content_rewrite(  # noqa: SLF001
                prepared_workspace,
                "ch-0001",
                current_version=2,
            )
            is False
        )
        mock_client.complete.assert_not_called()

    def test_rewrite_ignores_caller_prior_rewrite_count(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """caller 伪造 prior 不影响预算；本次 rewrite 由服务自行计为 1。

        构造:先把 ChapterCard 的 max_rewrites 改成 1,调用方伪造 prior=999。
        Orchestrator 第一轮审稿判 rewrite 时,
        rewrite_count(1)>=max(1) → 升人工。
        """
        # 覆盖章节卡:max_rewrites=1
        chapter_service.save_chapter_card(
            prepared_workspace, _make_card(max_rewrites=1)
        )
        mock_client.complete.side_effect = [
            _writer_response(content="重写稿 v1"),
            _reviewer_response(score=5.0),  # rewrite,但名额已满 → 升人工
        ]
        result = chapter_service.rewrite(
            prepared_workspace, "ch-0002", prior_rewrite_count=999
        )

        assert result.final_state == ChapterState.HUMAN_INTERVENTION
        assert result.escalation_reason == "rewrite_budget_exhausted"
        assert result.rewrite_count == 1

    def test_malformed_review_checkpoint_preserves_state_and_stops_provider(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """坏 constraint_results 在状态写入和 Provider 前硬拦截。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterORM, ChapterVersionORM
        from src.core.orchestrator.chapter_orchestrator import (
            RewriteCheckpointRecoveryError,
        )

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=[{"broken": True}],
        )

        with pytest.raises(RewriteCheckpointRecoveryError):
            service.rewrite(prepared_workspace, "ch-0001")

        mock_client.complete.assert_not_called()
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert chapter.status == "human_intervention"
            assert chapter.current_version == 1
            assert chapter.rewrite_count == 0
            assert session.query(ChapterVersionORM).count() == 1

    def test_missing_v1_file_preserves_state_and_stops_provider(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """SQLite 有 v1、正文文件缺失时同样零写入、零 Provider。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterORM, ChapterVersionORM
        from src.core.orchestrator.chapter_orchestrator import (
            RewriteCheckpointRecoveryError,
        )

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=False,
            constraint_results=None,
        )

        with pytest.raises(RewriteCheckpointRecoveryError):
            service.rewrite(prepared_workspace, "ch-0001")

        mock_client.complete.assert_not_called()
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert chapter.status == "human_intervention"
            assert chapter.current_version == 1
            assert chapter.rewrite_count == 0
            assert session.query(ChapterVersionORM).count() == 1

    @pytest.mark.parametrize("alias_name", ["v01.md", "v0.md", "v١.md"])
    def test_noncanonical_version_alias_preserves_checkpoint_and_stops_provider(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
        alias_name: str,
    ) -> None:
        """数字型非规范 vN 别名不得被折叠进 Rewrite 版本集合。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterORM, ChapterVersionORM
        from src.core.orchestrator.chapter_orchestrator import (
            RewriteCheckpointRecoveryError,
        )
        from src.core.workspace.workspace_paths import WorkspacePaths

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=None,
        )
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        chapter_dir = paths.chapter_dir("ch-0001")
        (chapter_dir / alias_name).write_text("歧义版本别名", encoding="utf-8")
        files_before = {
            path.name: path.read_bytes()
            for path in chapter_dir.iterdir()
            if path.is_file()
        }

        with pytest.raises(RewriteCheckpointRecoveryError, match="正文 checkpoint"):
            service.rewrite(prepared_workspace, "ch-0001")

        mock_client.complete.assert_not_called()
        assert {
            path.name: path.read_bytes()
            for path in chapter_dir.iterdir()
            if path.is_file()
        } == files_before
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert (
                chapter.status,
                chapter.current_version,
                chapter.rewrite_count,
            ) == ("human_intervention", 1, 0)
            assert session.query(ChapterVersionORM).count() == 1

    def test_non_utf8_version_preserves_checkpoint_and_stops_provider(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """正文 UTF-8 损坏统一包装成恢复错误，且不得产生任何写入。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterORM, ChapterVersionORM
        from src.core.orchestrator.chapter_orchestrator import (
            RewriteCheckpointRecoveryError,
        )
        from src.core.workspace.workspace_paths import WorkspacePaths

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=None,
        )
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        version_path = paths.chapter_version_file("ch-0001", "v1.md")
        version_path.write_bytes(b"\xff\xfe")
        bytes_before = version_path.read_bytes()

        with pytest.raises(RewriteCheckpointRecoveryError, match="正文 checkpoint"):
            service.rewrite(prepared_workspace, "ch-0001")

        mock_client.complete.assert_not_called()
        assert version_path.read_bytes() == bytes_before
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert (
                chapter.status,
                chapter.current_version,
                chapter.rewrite_count,
            ) == ("human_intervention", 1, 0)
            assert session.query(ChapterVersionORM).count() == 1

    def test_write_resume_rejects_noncanonical_version_before_provider(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """write 的 REVIEWING 恢复入口与显式 Rewrite 共用版本目录硬门。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterORM, ChapterVersionORM
        from src.core.orchestrator.chapter_orchestrator import (
            RewriteCheckpointRecoveryError,
        )
        from src.core.workspace.workspace_paths import WorkspacePaths

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=None,
        )
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        alias_path = paths.chapter_dir("ch-0001") / "v01.md"
        alias_path.write_text("歧义版本别名", encoding="utf-8")

        with pytest.raises(RewriteCheckpointRecoveryError, match="正文 checkpoint"):
            service.write(prepared_workspace, "ch-0001")

        mock_client.complete.assert_not_called()
        assert alias_path.read_text(encoding="utf-8") == "歧义版本别名"
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert (
                chapter.status,
                chapter.current_version,
                chapter.rewrite_count,
            ) == ("human_intervention", 1, 0)
            assert session.query(ChapterVersionORM).count() == 1

    def test_malformed_failed_constraints_preserves_state_and_stops_provider(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """failed_constraints 被写成字符串时不得拆成字符继续 Rewrite。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterORM, ChapterVersionORM
        from src.core.orchestrator.chapter_orchestrator import (
            RewriteCheckpointRecoveryError,
        )

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=[],
            failed_constraints="style_drift",
        )

        with pytest.raises(RewriteCheckpointRecoveryError):
            service.rewrite(prepared_workspace, "ch-0001")

        mock_client.complete.assert_not_called()
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert chapter.status == "human_intervention"
            assert chapter.current_version == 1
            assert chapter.rewrite_count == 0
            assert session.query(ChapterVersionORM).count() == 1

    def test_malformed_constraint_violation_preserves_state_and_stops_provider(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """结构化违规坏形状时不得丢掉警告后继续请求 Provider。"""
        from sqlalchemy.orm import Session

        from src.core.db.models import ChapterORM, ChapterVersionORM
        from src.core.orchestrator.chapter_orchestrator import (
            RewriteCheckpointRecoveryError,
        )

        service, engine = self._checkpoint_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            workspace_id=prepared_workspace,
            write_v1_file=True,
            constraint_results=[],
            constraint_violations=[{"broken": True}],
        )

        with pytest.raises(RewriteCheckpointRecoveryError):
            service.rewrite(prepared_workspace, "ch-0001")

        mock_client.complete.assert_not_called()
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert chapter.status == "human_intervention"
            assert chapter.current_version == 1
            assert chapter.rewrite_count == 0
            assert session.query(ChapterVersionORM).count() == 1


# ---------------------------------------------------------------------------
# 4. revise(不走 Orchestrator)
# ---------------------------------------------------------------------------


class TestRevise:
    def test_revise_creates_new_version_no_review(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
    ) -> None:
        """revise:在已有 v1 基础上落一版 v2(revise),不审稿、不动 final.md。"""
        # 先用 write 跑一轮把 v1 落出来(只跑 1 次审稿就 pass,产生 v1 + final.md)
        mock_client.complete.side_effect = [
            _writer_response(content="原稿"),
            _reviewer_response(score=8.0),
        ]
        chapter_service.write(prepared_workspace, "ch-0002")

        chapter_dir = (
            tmp_path / "workspaces" / "ws-demo" / "data" / "chapters" / "ch-0002"
        )
        # 此时 final.md 内容 = "原稿"
        assert (chapter_dir / "final.md").read_text(encoding="utf-8") == "原稿"

        # revise:LLM 只调 Reviser 一次
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = _contract_sequence(
            lambda messages: _localized_reviser_response(
                messages, replacement="修订稿"
            )
        )
        issue = ReviewIssue(
            paragraph_anchor="原稿",
            issue_type="pacing",
            description="节奏过缓",
            suggestion="压缩",
        )
        new_version = chapter_service.revise(
            prepared_workspace, "ch-0002", issues=[issue]
        )

        # 新版本号 = 2,creation_type=revise
        assert new_version.version_id == 2
        assert new_version.creation_type == "revise"
        assert new_version.content == "修订稿"
        # v2.md 真落盘
        assert (chapter_dir / "v2.md").read_text(encoding="utf-8") == "修订稿"
        # final.md 不动(revise 不参与状态机转换)
        assert (chapter_dir / "final.md").read_text(encoding="utf-8") == "原稿"
        # 只调了 Reviser 一次,没调 Reviewer
        assert mock_client.complete.call_count == 1
        revise_messages = mock_client.complete.call_args.kwargs["messages"]
        assert "局部修订约束合同(REVISER/VALIDATOR SSOT)" in revise_messages[1].content
        assert "plan_hash:" in revise_messages[1].content

    def test_revise_with_explicit_source_version_id(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
    ) -> None:
        """显式指定 source_version_id:基于 v1 修订,即便 v2 已存在。"""
        # 先 write 出 v1 + final
        mock_client.complete.side_effect = [
            _writer_response(content="v1 内容"),
            _reviewer_response(score=8.0),
        ]
        chapter_service.write(prepared_workspace, "ch-0002")

        # 再 revise 一次出 v2(默认基于 v1,因为 v1 是最新)
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = _contract_sequence(
            lambda messages: _localized_reviser_response(
                messages, replacement="基于 v1 改的 v2"
            )
        )
        issue = ReviewIssue(
            paragraph_anchor="v1 内容",
            issue_type="pacing",
            description="x",
            suggestion="y",
        )
        chapter_service.revise(prepared_workspace, "ch-0002", issues=[issue])

        # 现在显式指定 source_version_id=1,产出 v3,Reviser 收到 "v1 内容"
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = _contract_sequence(
            lambda messages: _localized_reviser_response(
                messages, replacement="基于 v1 改的 v3"
            )
        )
        new_version = chapter_service.revise(
            prepared_workspace, "ch-0002", issues=[issue], source_version_id=1
        )
        assert new_version.version_id == 3
        # 校验 Reviser 拿到的原文是 v1 而非 v2
        call_kwargs = mock_client.complete.call_args.kwargs
        user_msg = next(m for m in call_kwargs["messages"] if m.role == "user")
        assert "v1 内容" in user_msg.content
        assert "基于 v1 改的 v2" not in user_msg.content

    def test_explicit_revise_noop_is_rejected_before_new_version(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_response(content="原稿"),
            _reviewer_response(score=8.0),
        ]
        chapter_service.write(prepared_workspace, "ch-0002")

        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = _contract_sequence(
            lambda messages: _localized_reviser_response(
                messages,
                replacement="原稿",
            )
        )
        issue = ReviewIssue(
            paragraph_anchor="原稿",
            issue_type="pacing",
            description="节奏过缓",
            suggestion="压缩",
        )

        with pytest.raises(ProseRevisionNoProgressError, match="实质变化"):
            chapter_service.revise(
                prepared_workspace,
                "ch-0002",
                issues=[issue],
            )

        chapter_dir = (
            tmp_path / "workspaces" / "ws-demo" / "data" / "chapters" / "ch-0002"
        )
        assert not (chapter_dir / "v2.md").exists()
        assert mock_client.complete.call_count == 1

    def test_explicit_revise_rejects_exact_adjacent_long_duplicate_before_write(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
    ) -> None:
        kept = (
            "守门人逐项核对刻痕与封蜡，确认所有编号都对应原始登记。" * 12
        )[:260]
        target = (
            "访客站在门边等待，手里没有触碰任何机关。" * 12
        )[:260]
        source = f"{kept}\n\n{target}\n\n末段保持不变。"
        mock_client.complete.side_effect = [
            _writer_response(content=source),
            _reviewer_response(score=8.0),
        ]
        chapter_service.write(prepared_workspace, "ch-0002")

        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = _contract_sequence(
            lambda messages: _localized_reviser_response(
                messages, replacement=kept
            )
        )
        issue = ReviewIssue(
            paragraph_anchor=target[:30],
            issue_type="prose",
            description="补清楚过桥动作",
            suggestion="只改该段",
        )

        with pytest.raises(ProseRevisionContractError, match="相邻长段重复"):
            chapter_service.revise(
                prepared_workspace,
                "ch-0002",
                issues=[issue],
            )

        chapter_dir = (
            tmp_path / "workspaces" / "ws-demo" / "data" / "chapters" / "ch-0002"
        )
        assert not (chapter_dir / "v2.md").exists()
        assert mock_client.complete.call_count == 1

    def test_revise_no_source_raises(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """章节目录无任何版本 → ChapterServiceError("尚无任何版本")。"""
        with pytest.raises(ChapterServiceError, match="尚无任何版本"):
            chapter_service.revise(prepared_workspace, "ch-0002", issues=[])
        mock_client.complete.assert_not_called()

    def test_revise_explicit_source_not_exists(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """显式 source_version_id 对应文件不存在 → ChapterServiceError。"""
        # 先 write 出 v1
        mock_client.complete.side_effect = [
            _writer_response(content="v1"),
            _reviewer_response(score=8.0),
        ]
        chapter_service.write(prepared_workspace, "ch-0002")

        with pytest.raises(ChapterServiceError, match="源版本不存在"):
            chapter_service.revise(
                prepared_workspace,
                "ch-0002",
                issues=[],
                source_version_id=99,
            )


# ---------------------------------------------------------------------------
# 5. status:7 档(P2-B-17 起,fs fallback 路径)
# ---------------------------------------------------------------------------


class TestStatusFsFallback:
    """``session_factory=None`` 时(本 fixture 默认),``status()`` 走文件系统
    fallback 推断 7 档(``_fs_derive_state``):

    - 无 ``final.md`` → ``drafting``(无版本 / 有 ``v*.md`` 都归 drafting,
      P1 ``not_started`` / ``unknown`` 折算)
    - 有 ``final.md`` → ``approved``

    DB 路径(``session_factory`` 注入)在 ``TestStatusDbBacked`` 覆盖。
    """

    def test_status_no_versions_maps_to_drafting(
        self, chapter_service: ChapterService, prepared_workspace: str
    ) -> None:
        info = chapter_service.status(prepared_workspace, "ch-0002")
        assert info == ChapterStatusInfo(
            chapter_id="ch-0002",
            status="drafting",
            latest_version_id=None,
            has_final=False,
            version_count=0,
        )

    def test_status_approved_after_full_write(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_response(content="v1"),
            _reviewer_response(score=8.0),
        ]
        chapter_service.write(prepared_workspace, "ch-0002")

        info = chapter_service.status(prepared_workspace, "ch-0002")
        assert info.status == "approved"
        assert info.has_final is True
        assert info.latest_version_id == 1
        assert info.version_count == 1

    def test_status_versions_no_final_maps_to_drafting(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
    ) -> None:
        """有 ``v*.md`` 但无 ``final.md`` → ``drafting``(P1 ``unknown`` 折算
        到状态机起点;无 chapters 行说明状态机未启动)。
        """
        chapter_dir = (
            tmp_path / "workspaces" / "ws-demo" / "data" / "chapters" / "ch-0002"
        )
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "v1.md").write_text("手工 v1", encoding="utf-8")

        info = chapter_service.status(prepared_workspace, "ch-0002")
        assert info.status == "drafting"
        assert info.has_final is False
        assert info.version_count == 1
        assert info.latest_version_id == 1


class TestStatusDbBacked:
    """``session_factory`` 注入后,``status()`` 优先读 ``chapters`` 表
    (P2-B-17)。

    write 入口 upsert 行 + state callback 更新 status 列,跑完一次 write 后
    SQLite ``chapters.status='approved'``;同一个 chapter 的 ``status()``
    再调时返回 ``approved`` 而不是 fs fallback 的近似值。
    """

    def test_write_upserts_chapter_row_with_state_progression(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """write 跑完一轮 → ``chapters`` 表行存在 + ``status='approved'``。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base, ChapterORM

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            from src.core.workspace.workspace_paths import WorkspacePaths

            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        service.save_chapter_card("ws-demo", _make_card("ch-0001", chapter_number=1))

        mock_client.complete.side_effect = [
            _writer_response(content="v1"),
            _reviewer_response(score=8.0),
            _summary_response("E0001"),
        ]
        service.write("ws-demo", "ch-0001")

        # 直接读 SQLite 验证 chapters 行 + status
        with _Session(engines["ws-demo"]) as session:  # type: ignore[arg-type]
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            assert row.workspace_id == "ws-demo"
            assert row.status == "approved"
            assert row.approved_at is not None

        # status() 优先读库,返回 approved(与 fs 推断恰好一致,但路径来自 DB)
        info = service.status("ws-demo", "ch-0001")
        assert info.status == "approved"

    def test_status_reads_db_state_over_fs_fallback(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """无 final.md 但 chapters.status='reviewing' → status() 返 'reviewing'
        (而非 fs fallback 的 'drafting')。这是 DB-priority 的关键证据。
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from datetime import datetime, timezone

        from src.core.db import Base, ChapterORM, WorkspaceORM
        from src.core.workspace.workspace_paths import WorkspacePaths

        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )

        paths = WorkspacePaths(
            "ws-demo", workspaces_root=workspace_service.workspaces_root
        )
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        # 手工塞一行 chapters,模拟 write/rewrite 跑了一半 + 状态机停在 reviewing
        now = datetime.now(timezone.utc)
        with _Session(engine) as session:
            session.add(
                WorkspaceORM(
                    workspace_id="ws-demo",
                    display_name="x",
                    genre="scifi",
                    premise="y",
                    created_at=now,
                    last_active_at=now,
                    engine_version="0.1.0",
                    status="active",
                    total_chapters=0,
                    completed_chapters=0,
                    config_overrides={},
                )
            )
            session.add(
                ChapterORM(
                    chapter_id="ch-0002",
                    workspace_id="ws-demo",
                    current_version=1,
                    status="reviewing",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=None,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
            session.commit()

        def _factory(_ws: str) -> _Session:
            return _Session(engine)

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        # 章节卡也得有(status 内部不读 card,但 _validate_workspace 读 meta.json,
        # 已在 workspace_service.create 里建好)。
        info = service.status("ws-demo", "ch-0002")
        assert info.status == "reviewing"
        # fs 信号不变:无版本 / 无 final
        assert info.has_final is False
        assert info.version_count == 0


# ---------------------------------------------------------------------------
# B-01 hot-fix:list_review_history 从 review_results 表查
# ---------------------------------------------------------------------------


class TestListReviewHistory:
    """B-01 hot-fix:``list_review_history`` 从 ``review_results`` 表查持久化历史。

    替代 v1 期前端走进程内 task_registry 的丢失路径(重启 / 刷新都丢)。
    """

    def _make_service_with_session(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base
        from src.core.workspace.workspace_paths import WorkspacePaths

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        return service, engines, _Session

    def test_empty_workspace_returns_empty_list(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """无 chapter / 无 review → 返空列表(不抛)。"""
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        service, _engines, _ = self._make_service_with_session(
            workspace_service, mock_client
        )
        entries = service.list_review_history("ws-demo", "ch-nonexistent")
        assert entries == []

    def test_returns_review_history_with_revise_targets(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """seed 2 个 review,每个含 revise_target → 按 reviewed_at 升序返。"""
        from datetime import datetime, timezone

        from src.core.db import (
            ChapterORM,
            ReviewIssueORM,
            ReviewResultORM,
            WorkspaceORM,
        )

        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        service, engines, _Session = self._make_service_with_session(
            workspace_service, mock_client
        )
        # 触发 engine 建表(首次 _factory 调用)
        service.list_review_history("ws-demo", "ch-bootstrap")

        now = datetime.now(timezone.utc)
        with _Session(engines["ws-demo"]) as session:  # type: ignore[arg-type]
            # 确保 workspace + chapter 行存在
            ws_row = session.get(WorkspaceORM, "ws-demo")
            if ws_row is None:
                from src.services.asset_import import ensure_workspace_orm

                ensure_workspace_orm(session, workspace_service.info("ws-demo"))
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id="ws-demo",
                    current_version=2,
                    status="approved",
                    rewrite_count=0,
                    revise_count=1,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
            session.add(
                ReviewResultORM(
                    review_id="rev-1",
                    chapter_id="ch-0001",
                    version_reviewed=1,
                    reviewed_at=now.replace(microsecond=100000),
                    reviewer_model="claude-sonnet-4-6",
                    score_plot_progression=7.0,
                    score_tension_match=6.5,
                    score_prose_quality=7.2,
                    score_consistency=7.8,
                    score_hook_management=7.0,
                    score_voice_consistency=7.5,
                    score_overall=7.17,
                    hard_constraints_passed=True,
                    failed_constraints=[],
                    constraint_violations=[
                        {
                            "constraint_id": "e" * 64,
                            "domain": "world.unique_rules",
                            "source_path": "data/outline/world.json",
                            "source_hash": "f" * 64,
                            "rule_quote": "记忆抹除不可逆。",
                            "evidence_quote": "被抹除的记忆全部恢复。",
                            "severity": "soft",
                            "ambiguity": "历史映射样本",
                        }
                    ],
                    constraint_results=[
                        {
                            "constraint_id": "a" * 64,
                            "domain": "forbidden",
                            "source_path": "chapter_cards/ch-0001.forbidden",
                            "source_hash": "",
                            "rule_text": "禁止提前揭示最终真相",
                            "status": "fail",
                            "details": "正文已经揭示",
                        }
                    ],
                    hard_constraint_regressions=["a" * 64],
                    suggestions=["s1"],
                    verdict="revise",
                )
            )
            session.add(
                ReviewResultORM(
                    review_id="rev-2",
                    chapter_id="ch-0001",
                    version_reviewed=2,
                    reviewed_at=now.replace(microsecond=200000),
                    reviewer_model="claude-sonnet-4-6",
                    score_plot_progression=8.0,
                    score_tension_match=7.5,
                    score_prose_quality=7.8,
                    score_consistency=8.0,
                    score_hook_management=7.8,
                    score_voice_consistency=7.8,
                    score_overall=7.81,
                    hard_constraints_passed=True,
                    failed_constraints=[],
                    suggestions=[],
                    verdict="pass",
                )
            )
            session.add(
                ReviewIssueORM(
                    review_id="rev-1",
                    role="revise_target",
                    position=0,
                    paragraph_anchor="开头 30 字",
                    issue_type="pacing",
                    description="节奏太慢",
                    suggestion="删 2 段",
                )
            )
            session.add(
                ReviewIssueORM(
                    review_id="rev-1",
                    role="issue",
                    position=1,
                    paragraph_anchor="中段 50 字",
                    issue_type="hook",
                    description="钩子无力",
                    suggestion="加悬念",
                )
            )
            session.commit()

        entries = service.list_review_history("ws-demo", "ch-0001")
        assert len(entries) == 2
        # 升序:rev-1 在前,rev-2 在后
        assert entries[0]["version_reviewed"] == 1
        assert entries[0]["verdict"] == "revise"
        assert entries[0]["score_overall"] == 7.17
        assert entries[0]["hard_constraints_passed"] is True
        assert entries[0]["constraint_violations"][0]["severity"] == "soft"
        assert (
            entries[0]["constraint_violations"][0]["rule_quote"] == "记忆抹除不可逆。"
        )
        assert entries[0]["constraint_results"][0]["constraint_id"] == "a" * 64
        assert entries[0]["hard_constraint_regressions"] == ["a" * 64]
        # revise_targets 仅含 role='revise_target',不含 role='issue'
        assert len(entries[0]["revise_targets"]) == 1
        assert entries[0]["revise_targets"][0]["issue_type"] == "pacing"
        assert entries[1]["version_reviewed"] == 2
        assert entries[1]["verdict"] == "pass"
        assert entries[1]["constraint_violations"] == []
        assert entries[1]["constraint_results"] == []
        assert entries[1]["hard_constraint_regressions"] == []
        # pass verdict 无 revise_target
        assert entries[1]["revise_targets"] == []

    def test_no_session_factory_returns_empty(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """session_factory=None 时返空列表(无表可查)。"""
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=None,
        )
        assert service.list_review_history("ws-demo", "ch-0001") == []


# ---------------------------------------------------------------------------
# A-03 hot-fix:workspace.completed_chapters 同步翻
# ---------------------------------------------------------------------------


class TestCompletedChaptersCounter:
    """A-03 hot-fix:``WorkspaceORM.completed_chapters`` 在首次 approved 时同步翻。

    覆盖:
    - write → approved 首次 +1
    - 同章二次 write → 已 approved 不重复 +1(approved_at 已存在)
    - session_factory=None → noop
    """

    def test_write_first_approve_increments_counter(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """write 首次进 approved → ``workspaces.completed_chapters`` 0 → 1。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base, WorkspaceORM
        from src.core.workspace.workspace_paths import WorkspacePaths

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        service.save_chapter_card("ws-demo", _make_card("ch-0001", chapter_number=1))

        mock_client.complete.side_effect = [
            _writer_response(content="v1"),
            _reviewer_response(score=8.0),
            _summary_response("E0001"),
        ]
        service.write("ws-demo", "ch-0001")

        # 实证 workspace.completed_chapters 翻成 1
        with _Session(engines["ws-demo"]) as session:  # type: ignore[arg-type]
            ws_row = session.get(WorkspaceORM, "ws-demo")
            assert ws_row is not None
            assert ws_row.completed_chapters == 1

    def test_re_write_already_approved_does_not_double_count(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """二次进 approved(approved_at 已填) → 计数不再 +1。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base, WorkspaceORM
        from src.core.workspace.workspace_paths import WorkspacePaths
        from src.services.chapter_service import ChapterStateValue

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        # 直接调 _upsert_chapter_state 两次进 approved
        state: ChapterStateValue = "approved"
        service._upsert_chapter_state("ws-demo", "ch-0002", state=state)
        service._upsert_chapter_state("ws-demo", "ch-0002", state=state)

        with _Session(engines["ws-demo"]) as session:  # type: ignore[arg-type]
            ws_row = session.get(WorkspaceORM, "ws-demo")
            assert ws_row is not None
            # 仅首次 +1,二次不重复
            assert ws_row.completed_chapters == 1

    def test_write_first_approve_syncs_meta_json(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """v1.0.x A2-03 续修:write 首次 approved → meta.json completed_chapters 0 → 1。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base
        from src.core.workspace.meta import read_meta
        from src.core.workspace.workspace_paths import WorkspacePaths

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        service.save_chapter_card("ws-demo", _make_card("ch-0001", chapter_number=1))

        mock_client.complete.side_effect = [
            _writer_response(content="v1"),
            _reviewer_response(score=8.0),
            _summary_response("E0001"),
        ]
        service.write("ws-demo", "ch-0001")

        paths = WorkspacePaths(
            "ws-demo", workspaces_root=workspace_service.workspaces_root
        )
        workspace = read_meta(paths.meta_json)
        assert workspace.completed_chapters == 1

    def test_re_write_does_not_double_count_meta_json(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """二次进 approved → meta.json 不重复 +1(对齐 ORM 不双计语义)。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base
        from src.core.workspace.meta import read_meta
        from src.core.workspace.workspace_paths import WorkspacePaths
        from src.services.chapter_service import ChapterStateValue

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        state: ChapterStateValue = "approved"
        service._upsert_chapter_state("ws-demo", "ch-0002", state=state)
        service._upsert_chapter_state("ws-demo", "ch-0002", state=state)

        paths = WorkspacePaths(
            "ws-demo", workspaces_root=workspace_service.workspaces_root
        )
        workspace = read_meta(paths.meta_json)
        assert workspace.completed_chapters == 1


# ---------------------------------------------------------------------------
# 6. switch_version
# ---------------------------------------------------------------------------


class TestAutoBackupHook:
    """P6-B-07:approve transaction 后同步调 ``BackupService.create_auto_backup``。

    覆盖 plan §10 happy 字面要求"chapter approve → 实证 backups/auto_*_ch-0001.zip
    落盘 + 解压验 schema"。
    """

    def test_approve_triggers_auto_backup_zip_landing(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """write 跑到 approved → ``backups/auto_*_ch-0001.zip`` 文件落盘 +
        内含 db / outline / cards / meta.json。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base
        from src.core.workspace.workspace_paths import WorkspacePaths
        from src.services.backup_service import BackupService

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        backup_svc = BackupService(workspace_service=workspace_service, ws_manager=None)
        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
            backup_service=backup_svc,
        )
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        service.save_chapter_card("ws-demo", _make_card("ch-0001", chapter_number=1))

        mock_client.complete.side_effect = [
            _writer_response(content="v1"),
            _reviewer_response(score=8.0),
            _summary_response("E0001"),
        ]
        service.write("ws-demo", "ch-0001")

        # 实证 backups/auto_*_ch-0001.zip 落盘
        backups_dir = workspace_service.workspaces_root / "ws-demo" / "backups"
        autos = list(backups_dir.glob("auto_*_ch-0001.zip"))
        assert len(autos) == 1, "期望恰好 1 份 auto backup 落盘"

        # 解压验内容含 db + outline + cards + backup_meta.json
        import json as _json
        import zipfile as _zip

        with _zip.ZipFile(autos[0], "r") as zf:
            names = set(zf.namelist())
            assert "backup_meta.json" in names
            assert "data/novel.db" in names
            meta = _json.loads(zf.read("backup_meta.json").decode("utf-8"))
            assert meta["backup_type"] == "auto"
            assert meta["chapter_id"] == "ch-0001"

    def test_backup_failure_does_not_break_approve(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """BackupService.create_auto_backup 抛错 → swallow + structlog,
        approve 流程不中断。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base, ChapterORM
        from src.core.workspace.workspace_paths import WorkspacePaths

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        # fake backup_service:create_auto_backup 必抛
        class _BoomBackup:
            def create_auto_backup(self, *args, **kwargs):  # noqa: ANN001 / D401
                raise RuntimeError("simulated backup failure")

        # 必须 monkeypatch isinstance 检查 — 用真 BackupService 子类
        from src.services.backup_service import BackupService

        class _RaisingBackup(BackupService):
            def __init__(self) -> None:
                pass

            def create_auto_backup(self, *args, **kwargs):  # noqa: ANN001
                raise RuntimeError("simulated backup failure")

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
            backup_service=_RaisingBackup(),
        )
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        service.save_chapter_card("ws-demo", _make_card("ch-0001", chapter_number=1))

        mock_client.complete.side_effect = [
            _writer_response(content="v1"),
            _reviewer_response(score=8.0),
            _summary_response("E0001"),
        ]
        # 不抛错 → backup 失败被 swallow
        service.write("ws-demo", "ch-0001")

        # approve 仍然完成(DB 行 status=approved)
        with _Session(engines["ws-demo"]) as session:  # type: ignore[arg-type]
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            assert row.status == "approved"


class TestSwitchVersion:
    def test_switch_version_marks_final(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
    ) -> None:
        """switch_version(v1) 把 v1.md 拷成 final.md 并返回路径。"""
        # 跑一轮 write(v1 + final)
        mock_client.complete.side_effect = [
            _writer_response(content="v1 内容"),
            _reviewer_response(score=8.0),
        ]
        chapter_service.write(prepared_workspace, "ch-0002")
        # 再 revise 出 v2
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = _contract_sequence(
            lambda messages: _localized_reviser_response(
                messages, replacement="v2 内容"
            )
        )
        chapter_service.revise(
            prepared_workspace,
            "ch-0002",
            issues=[
                ReviewIssue(
                    paragraph_anchor="v1 内容",
                    issue_type="x",
                    description="x",
                    suggestion="x",
                )
            ],
        )

        # 把 final 切换到 v2
        final_path = chapter_service.switch_version(prepared_workspace, "ch-0002", 2)
        chapter_dir = (
            tmp_path / "workspaces" / "ws-demo" / "data" / "chapters" / "ch-0002"
        )
        assert final_path == chapter_dir / "final.md"
        assert final_path.read_text(encoding="utf-8") == "v2 内容"

    def test_switch_version_missing_raises(
        self, chapter_service: ChapterService, prepared_workspace: str
    ) -> None:
        # P1-B-19 修正:让 ChapterVersioningError 自然冒泡,而非外包成
        # ChapterServiceError("版本切换失败...") —— 后者会被全局翻译表(P1-B-17)
        # 按"含'不存在'"误归到 CHAPTER_NOT_FOUND (404),丢失"版本 vs 章节"语义
        # 精度。版本不存在归 CHAPTER_VERSION_CONFLICT (409) 才准确。
        from src.core.chapter_versioning import ChapterVersioningError

        with pytest.raises(ChapterVersioningError, match="无法标记 final"):
            chapter_service.switch_version(prepared_workspace, "ch-0002", 99)


# ---------------------------------------------------------------------------
# 7. 章节卡 IO
# ---------------------------------------------------------------------------


class TestChapterCardIO:
    def test_save_then_read_round_trip(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        card = _make_card(title="特殊章节")
        path = chapter_service.save_chapter_card("ws-demo", card)
        assert path.is_file()

        loaded = chapter_service.read_chapter_card("ws-demo", "ch-0002")
        assert loaded.title == "特殊章节"
        assert loaded.chapter_id == "ch-0002"

    def test_read_missing_card_raises(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        with pytest.raises(ChapterServiceError, match="章节卡不存在"):
            chapter_service.read_chapter_card("ws-demo", "ghost")

    def test_read_invalid_json_raises(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
        tmp_path: Path,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        bad_path = (
            tmp_path / "workspaces" / "ws-demo" / "data" / "cards" / "ch-bad.json"
        )
        bad_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ChapterServiceError, match="不是合法 JSON"):
            chapter_service.read_chapter_card("ws-demo", "ch-bad")

    def test_read_invalid_schema_raises(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
        tmp_path: Path,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        bad_path = tmp_path / "workspaces" / "ws-demo" / "data" / "cards" / "ch-x.json"
        bad_path.write_text(
            json.dumps({"chapter_id": "ch-x"}), encoding="utf-8"
        )  # 缺一堆必填字段
        with pytest.raises(ChapterServiceError, match="ChapterCard schema"):
            chapter_service.read_chapter_card("ws-demo", "ch-x")


# ---------------------------------------------------------------------------
# 8. WorkspaceService 注入校验
# ---------------------------------------------------------------------------


class TestWorkspaceValidation:
    def test_status_on_missing_workspace_raises(
        self, chapter_service: ChapterService
    ) -> None:
        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            chapter_service.status("ghost", "ch-0001")

    def test_revise_on_missing_workspace_raises(
        self, chapter_service: ChapterService
    ) -> None:
        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            chapter_service.revise("ghost", "ch-0001", issues=[])

    def test_save_card_on_missing_workspace_raises(
        self, chapter_service: ChapterService
    ) -> None:
        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            chapter_service.save_chapter_card("ghost", _make_card())


# ---------------------------------------------------------------------------
# 9. P3-B-13 continuity 业务链路 wiring(ch>=2 时从上一章 final.md 重抽)
# ---------------------------------------------------------------------------


class TestContinuityWiring:
    """P3-B-13:`_orchestrate` ch>=2 路径调 ``build_continuity_context``,model_copy
    覆盖 ``card.continuity_context`` 后传 Orchestrator;ch=1 跳过;上一章 final.md
    缺失 / 上一章卡缺失 / 抽取失败均 swallow + log + 返原 card 不阻塞主流水线。
    """

    @staticmethod
    def _seed_prev_chapter(
        tmp_path: Path,
        chapter_service: ChapterService,
        workspace_id: str,
        *,
        prev_final_text: str | None = "上一章末尾,他握紧剑柄,目光不离对面那道身影。",
    ) -> Path:
        """在 ``workspace_id`` 下落 ch-0001 章节卡;``prev_final_text`` 给值时
        同时落 ``data/chapters/ch-0001/final.md``,返其 Path。"""
        prev_card = _make_card(chapter_id="ch-0001", chapter_number=1)
        chapter_service.save_chapter_card(workspace_id, prev_card)
        prev_final = (
            tmp_path
            / "workspaces"
            / workspace_id
            / "data"
            / "chapters"
            / "ch-0001"
            / "final.md"
        )
        if prev_final_text is not None:
            prev_final.parent.mkdir(parents=True, exist_ok=True)
            prev_final.write_text(prev_final_text, encoding="utf-8")
        return prev_final

    def test_first_chapter_skips_continuity_refresh(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``chapter_number=1`` → ``build_continuity_context`` 不调,卡片原样传入。"""
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        chapter_service.save_chapter_card(
            "ws-demo",
            _make_card(chapter_id="ch-0001", chapter_number=1),
        )
        spy = MagicMock()
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context", spy
        )
        mock_client.complete.side_effect = [
            _writer_response(content="首章正文"),
            _reviewer_response(score=8.0),
        ]

        chapter_service.write("ws-demo", "ch-0001")
        spy.assert_not_called()

    def test_second_chapter_invokes_when_prev_final_exists(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ch=2 且 ch-0001/final.md 已落 → ``build_continuity_context`` 被调,
        返回值通过 ``model_copy`` 覆盖到传给 Orchestrator 的 card 上。"""
        prev_final = self._seed_prev_chapter(
            tmp_path, chapter_service, prepared_workspace
        )
        refreshed = _make_continuity(
            prev_chapter_tail="LLM_REFRESHED_TAIL",
            unfinished_actions=["阵眼真凶身份未揭"],
        )
        spy = MagicMock(return_value=refreshed)
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context", spy
        )
        captured: dict[str, ChapterCard] = {}

        def _capture_orchestrate(*, chapter_card: ChapterCard, **_kwargs):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            captured["card"] = chapter_card
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )

        chapter_service.write(prepared_workspace, "ch-0002")

        spy.assert_called_once()
        kwargs = spy.call_args.kwargs
        assert kwargs["prev_content"] == prev_final.read_text(encoding="utf-8")
        assert kwargs["current_card"].chapter_id == "ch-0002"
        # router 入参在 legacy 路径无 router_factory 时为 None;client + model
        # 走 legacy 兜底
        assert kwargs["router"] is None
        assert kwargs["client"] is mock_client
        assert kwargs["model"] == "claude-sonnet-4-6"
        # 传给 Orchestrator 的 card 已被 refreshed 覆盖
        assert (
            captured["card"].continuity_context.prev_chapter_tail
            == "LLM_REFRESHED_TAIL"
        )
        assert captured["card"].continuity_context.unfinished_actions == [
            "阵眼真凶身份未揭"
        ]

    def test_skip_when_prev_final_missing(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ch=2 + ch-0001 卡存在但 ``final.md`` 缺失(尚未 approved)→ 跳过 +
        卡片原样传入 + spy 不被调用(避免拿到空 prev_content 让 LLM 误判)。"""
        self._seed_prev_chapter(
            tmp_path,
            chapter_service,
            prepared_workspace,
            prev_final_text=None,
        )
        spy = MagicMock()
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context", spy
        )
        mock_client.complete.side_effect = [
            _writer_response(content="第二章正文"),
            _reviewer_response(score=8.0),
        ]

        chapter_service.write(prepared_workspace, "ch-0002")
        spy.assert_not_called()

    def test_skip_when_prev_card_not_found(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ch=2 但 ``cards_dir`` 中没有 chapter_number=1 的章节卡(只有 ch-0002)
        → 跳过,spy 不被调用。"""
        spy = MagicMock()
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context", spy
        )
        mock_client.complete.side_effect = [
            _writer_response(content="第二章正文"),
            _reviewer_response(score=8.0),
        ]

        chapter_service.write(prepared_workspace, "ch-0002")
        spy.assert_not_called()

    def test_llm_failure_falls_back_to_rule_path(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v1.0.x wave 5 session 3 主题 l:LLM 路径抛 ValueError(含
        ``ContinuityExtractorError``)→ 自动降级走 rule 路径(strategy='rule'),
        Writer 拿到 rule 抽提的 refreshed card 而非占位原 card。"""
        self._seed_prev_chapter(tmp_path, chapter_service, prepared_workspace)
        # 首调(LLM)抛 ValueError;第二调(rule)正常返 refreshed
        rule_refreshed = _make_continuity(
            prev_chapter_tail="RULE_FALLBACK_TAIL",
            unfinished_actions=["rule_action"],
        )
        spy = MagicMock(
            side_effect=[ValueError("LLM JSON 解析失败(模拟)"), rule_refreshed]
        )
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context", spy
        )
        captured: dict[str, ChapterCard] = {}

        def _capture_orchestrate(*, chapter_card: ChapterCard, **_kwargs):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            captured["card"] = chapter_card
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )

        # 不应冒泡 ValueError
        chapter_service.write(prepared_workspace, "ch-0002")
        # spy 被调 2 次:LLM 一次 + rule fallback 一次
        assert spy.call_count == 2
        # 第二次调用强制 strategy='rule'
        second_call_kwargs = spy.call_args_list[1].kwargs
        assert second_call_kwargs["strategy"] == "rule"
        # Orchestrator 拿到 rule fallback 后的 refreshed card
        assert (
            captured["card"].continuity_context.prev_chapter_tail
            == "RULE_FALLBACK_TAIL"
        )

    def test_double_failure_swallows_and_uses_original_card(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v1.0.x wave 5 session 3 主题 l:LLM 路径 + rule fallback 路径都抛 →
        最终 swallow + log + 主流水线继续,Orchestrator 拿到原 card。"""
        self._seed_prev_chapter(tmp_path, chapter_service, prepared_workspace)
        spy = MagicMock(side_effect=ValueError("LLM JSON 解析失败(模拟)"))
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context", spy
        )
        captured: dict[str, ChapterCard] = {}

        def _capture_orchestrate(*, chapter_card: ChapterCard, **_kwargs):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            captured["card"] = chapter_card
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )

        chapter_service.write(prepared_workspace, "ch-0002")
        # spy 被调 2 次(LLM + rule fallback),两次都抛 → 兜底
        assert spy.call_count == 2
        # 原 card.continuity_context 未被覆盖(_make_continuity 默认值)
        assert (
            captured["card"].continuity_context.prev_chapter_tail
            == "他握紧剑柄,目光不离对面那道身影。"
        )

    def test_refreshed_continuity_persisted_to_card_json(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v1.0.x wave 5 session 3 主题 m:成功重抽后,refreshed
        ``ContinuityContext`` 写回 ``data/cards/<chapter_id>.json``,UI 后续
        读章节卡时拿到的就是 Writer 实际消费的真值。"""
        self._seed_prev_chapter(tmp_path, chapter_service, prepared_workspace)
        refreshed = _make_continuity(
            prev_chapter_tail="PERSISTED_REAL_TAIL_800_CHARS",
            unfinished_actions=["阵眼真凶未揭"],
        )
        spy = MagicMock(return_value=refreshed)
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context", spy
        )

        def _capture_orchestrate(*, chapter_card: ChapterCard, **_kwargs):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )

        chapter_service.write(prepared_workspace, "ch-0002")

        # 验证章节卡 JSON 已被覆盖写回为 refreshed
        persisted = chapter_service.read_chapter_card(prepared_workspace, "ch-0002")
        assert (
            persisted.continuity_context.prev_chapter_tail
            == "PERSISTED_REAL_TAIL_800_CHARS"
        )
        assert persisted.continuity_context.unfinished_actions == ["阵眼真凶未揭"]

    def test_v1_refresh_preserves_frozen_time_gap_and_book_clock(
        self,
        chapter_service: ChapterService,
        prepared_workspace: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """旧 continuity refresh 可更新衔接内容，但不得改写 v1 已冻结书内时钟。"""
        self._seed_prev_chapter(tmp_path, chapter_service, prepared_workspace)
        projection = make_core_state_projection(
            book_clock=BookClockInterval(minimum_minutes=60, maximum_minutes=120)
        )
        clock = build_book_clock_constraint(
            time_gap="次日",
            previous_interval=projection.book_clock,
            is_first_chapter=False,
        )
        card = _make_card(
            core_state_contract_version=1,
            core_state_projection_hash=projection.projection_hash,
            book_clock_constraint=clock,
            continuity_context=_make_continuity(time_gap="次日"),
        )
        chapter_service.save_chapter_card(prepared_workspace, card)
        refreshed = _make_continuity(
            prev_chapter_tail="REFRESHED_WITH_DIFFERENT_GAP",
            unfinished_actions=["继续追查"],
            time_gap="三个月后",
        )
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context",
            MagicMock(return_value=refreshed),
        )

        paths = chapter_service._validate_workspace(prepared_workspace)
        result = chapter_service._maybe_refresh_continuity(paths, card, None)

        assert result.continuity_context.prev_chapter_tail == (
            "REFRESHED_WITH_DIFFERENT_GAP"
        )
        assert result.continuity_context.time_gap == "次日"
        assert result.book_clock_constraint == clock
        persisted = chapter_service.read_chapter_card(
            prepared_workspace, "ch-0002"
        )
        assert persisted.continuity_context.time_gap == "次日"
        assert persisted.book_clock_constraint == clock

    def test_persist_failure_swallowed_returns_refreshed_card(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v1.0.x wave 5 session 3 主题 m 兜底:写回失败(OSError)swallow + log,
        Writer 仍拿到 refreshed card(主流水线不阻塞)。"""
        self._seed_prev_chapter(tmp_path, chapter_service, prepared_workspace)
        refreshed = _make_continuity(
            prev_chapter_tail="REFRESHED_BUT_PERSIST_FAILED",
            unfinished_actions=["x"],
        )
        spy = MagicMock(return_value=refreshed)
        monkeypatch.setattr(
            "src.services.chapter_service.build_continuity_context", spy
        )
        # 把 Path.write_text 替换成抛 OSError,模拟磁盘写失败
        original_write_text = Path.write_text

        def _failing_write_text(self: Path, *args, **kwargs):
            if self.suffix == ".tmp" and self.stem.startswith("ch-0002"):
                raise OSError("simulated disk write failure")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", _failing_write_text)

        captured: dict[str, ChapterCard] = {}

        def _capture_orchestrate(*, chapter_card: ChapterCard, **_kwargs):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            captured["card"] = chapter_card
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )

        # 不应冒泡 OSError
        chapter_service.write(prepared_workspace, "ch-0002")
        # Writer 仍拿到 refreshed 内存对象(写回失败不影响内存返值)
        assert (
            captured["card"].continuity_context.prev_chapter_tail
            == "REFRESHED_BUT_PERSIST_FAILED"
        )


# ---------------------------------------------------------------------------
# E6:批量 continuity backfill(v1.1 w3-s7)
# ---------------------------------------------------------------------------


class TestE6RefreshContinuityAll:
    """``refresh_continuity_all`` 聚合逻辑:首章 skip / 变化判 refreshed /
    未变判 unchanged / 坏卡 warn-skip / 空工作区返空 / 按 chapter_number 升序。

    单章 ``_maybe_refresh_continuity`` 内部(LLM→rule fail-open + 写回卡 JSON)
    由既有测试覆盖;本类聚焦 E6 新增的批量遍历 + 分类 + 计数 + 排序逻辑。
    """

    def _create_ws(self, workspace_service: WorkspaceService) -> str:
        workspace_service.create(
            "ws-demo",
            display_name="科幻测试本",
            genre="scifi",
            premise="人类在月球建殖民地",
            total_chapters=120,
        )
        return "ws-demo"

    def test_empty_workspace_returns_empty(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
    ) -> None:
        ws = self._create_ws(workspace_service)
        result = chapter_service.refresh_continuity_all(ws)
        assert result.total_scanned == 0
        assert result.refreshed_count == 0
        assert result.items == []

    def test_first_chapter_skipped_no_llm_call(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        ws = self._create_ws(workspace_service)
        chapter_service.save_chapter_card(
            ws, _make_card(chapter_id="ch-0001", chapter_number=1)
        )
        result = chapter_service.refresh_continuity_all(ws)
        assert result.total_scanned == 1
        assert result.refreshed_count == 0
        assert result.items[0].outcome == "skipped_first"
        # 首章不触发 LLM(_maybe_refresh_continuity ch<2 直接返)
        mock_client.complete.assert_not_called()

    def test_refreshed_unchanged_mix_sorted(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ch-0001 skip / ch-0002 内容变 → refreshed / ch-0003 未变 → unchanged。"""
        ws = self._create_ws(workspace_service)
        # 乱序保存,验证结果按 chapter_number 升序
        chapter_service.save_chapter_card(
            ws, _make_card(chapter_id="ch-0003", chapter_number=3)
        )
        chapter_service.save_chapter_card(
            ws, _make_card(chapter_id="ch-0001", chapter_number=1)
        )
        chapter_service.save_chapter_card(
            ws, _make_card(chapter_id="ch-0002", chapter_number=2)
        )

        def _stub_refresh(paths, card, router):  # noqa: ANN001 — 实例属性 stub 不绑 self
            if card.chapter_number == 2:
                return card.model_copy(
                    update={
                        "continuity_context": _make_continuity(
                            prev_chapter_tail="REFRESHED_TAIL"
                        )
                    }
                )
            return card  # ch-0003 原样返 → unchanged

        monkeypatch.setattr(chapter_service, "_maybe_refresh_continuity", _stub_refresh)

        result = chapter_service.refresh_continuity_all(ws)
        assert result.total_scanned == 3
        assert result.refreshed_count == 1
        assert [(i.chapter_id, i.outcome) for i in result.items] == [
            ("ch-0001", "skipped_first"),
            ("ch-0002", "refreshed"),
            ("ch-0003", "unchanged"),
        ]

    def test_ch2_without_prev_final_is_unchanged(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
    ) -> None:
        """真路径:ch-0002 但上一章未 approved(无 final.md)→ _maybe_refresh
        返原 card → unchanged(fail-open,不冒泡)。"""
        ws = self._create_ws(workspace_service)
        chapter_service.save_chapter_card(
            ws, _make_card(chapter_id="ch-0001", chapter_number=1)
        )
        chapter_service.save_chapter_card(
            ws, _make_card(chapter_id="ch-0002", chapter_number=2)
        )
        result = chapter_service.refresh_continuity_all(ws)
        assert result.refreshed_count == 0
        outcomes = {i.chapter_id: i.outcome for i in result.items}
        assert outcomes == {
            "ch-0001": "skipped_first",
            "ch-0002": "unchanged",
        }

    def test_invalid_card_warn_skipped(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
    ) -> None:
        """坏卡 JSON 不入 items 也不计 total_scanned(对齐 list_chapters lenient)。"""
        ws = self._create_ws(workspace_service)
        chapter_service.save_chapter_card(
            ws, _make_card(chapter_id="ch-0001", chapter_number=1)
        )
        cards_dir = chapter_service._validate_workspace(ws).cards_dir
        (cards_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
        result = chapter_service.refresh_continuity_all(ws)
        # 只数到合法的 ch-0001,坏卡被跳过
        assert result.total_scanned == 1
        assert [i.chapter_id for i in result.items] == ["ch-0001"]


# ---------------------------------------------------------------------------
# H7:章节版本风格指纹时序(v1.1 w3-s7)
# ---------------------------------------------------------------------------


class TestH7StyleFingerprintSeries:
    """``style_fingerprint_series``:按 version_id 升序派生 4 数值维度 +
    dominant_pov;旧版本 null fingerprint → has_fingerprint=False + 维度 None;
    无 session / 章节不存在 → 空列表。
    """

    def _make_service_with_session(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base
        from src.core.workspace.workspace_paths import WorkspacePaths

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        return service, engines, _Session

    def test_no_session_returns_empty(
        self,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
    ) -> None:
        """session_factory=None(默认 fixture)→ 空列表。"""
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        assert chapter_service.style_fingerprint_series("ws-demo", "ch-0002") == []

    def test_series_with_drift_and_null_version(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """v1 第三人称 / v2 第一人称(漂移)/ v3 无指纹 → 派生维度 + 排序正确。"""
        from datetime import datetime, timezone

        from src.core.db import ChapterORM, ChapterVersionORM, WorkspaceORM
        from src.core.rules.style_fingerprint import compute_style_fingerprint
        from src.services.asset_import import ensure_workspace_orm

        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        service, engines, _Session = self._make_service_with_session(
            workspace_service, mock_client
        )
        service.style_fingerprint_series("ws-demo", "ch-bootstrap")  # 触发建表

        third_fp = compute_style_fingerprint(
            "他走进残阵。他握紧剑柄,目光不离对面那道身影。"
            "她站在远处,他们都没有说话。他仿佛看见了过往。"
        )
        first_fp = compute_style_fingerprint(
            "我走进残阵。我握紧剑柄,目光不离对面那道身影。"
            "我们都没有说话。我仿佛看见了过往。"
        )
        now = datetime.now(timezone.utc)
        with _Session(engines["ws-demo"]) as session:  # type: ignore[arg-type]
            if session.get(WorkspaceORM, "ws-demo") is None:
                ensure_workspace_orm(session, workspace_service.info("ws-demo"))
            session.add(
                ChapterORM(
                    chapter_id="ch-0002",
                    workspace_id="ws-demo",
                    current_version=3,
                    status="approved",
                    rewrite_count=1,
                    revise_count=0,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
            for vid, ctype, fp in [
                (1, "draft", third_fp),
                (2, "rewrite", first_fp),
                (3, "rewrite", None),  # 旧版本无指纹
            ]:
                session.add(
                    ChapterVersionORM(
                        chapter_id="ch-0002",
                        version_id=vid,
                        content="正文",
                        word_count=4,
                        created_at=now,
                        creation_type=ctype,
                        model_used="claude-sonnet-4-6",
                        review_score=None,
                        notes="",
                        style_fingerprint=fp,
                    )
                )
            session.commit()

        points = service.style_fingerprint_series("ws-demo", "ch-0002")
        assert [p.version_id for p in points] == [1, 2, 3]  # 升序
        # v1 第三人称主导
        assert points[0].has_fingerprint is True
        assert points[0].dominant_pov == "third_person"
        assert points[0].paragraph_length_mean is not None
        assert points[0].simile_density is not None  # "仿佛" 命中比喻
        # v2 第一人称主导(漂移)
        assert points[1].dominant_pov == "first_person"
        # v3 无指纹 → 全 None
        assert points[2].has_fingerprint is False
        assert points[2].dominant_pov is None
        assert points[2].paragraph_length_mean is None
        assert points[2].exclamation_density is None

    def test_nonexistent_chapter_returns_empty(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        service, _engines, _ = self._make_service_with_session(
            workspace_service, mock_client
        )
        assert service.style_fingerprint_series("ws-demo", "ch-missing") == []


class TestProgressionConsistencyWiringS10:
    """v1.1 wave 5 s10② 接线防回退:ChapterService 解析境界一致性门控 + 透传 orchestrate_chapter。

    模块② 是**运行时注入**(非 card 静态字段):writer 注入"截止上一章境界"防漂移 +
    extractor 锚定抽进阶等级,都靠 ``_orchestrate`` 解析门控后透传 ``orchestrate_chapter``
    → write_chapter / extract。本组防未来改 ``_orchestrate`` / ``_resolve_progression_consistency``
    断了解析或透传 → 模块② 在生产中静默失效(§2.22),而 §3 启动 audit 机制层 mock 测照不出。
    """

    def test_resolve_progression_consistency_gating_by_genre(
        self, workspace_service: WorkspaceService, mock_client: MagicMock
    ) -> None:
        """门控解析:古典仙侠 auto→(True,境界);热血战斗 auto→(False,战力)
        (爽文/热血境界一致性 auto OFF 防误报,但 label 题材化);非 3 题材→(False,境界)。"""
        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
        )
        workspace_service.create(
            "ws-xianxia",
            display_name="x",
            genre="x",
            premise="x",
            style_template="classical_xianxia",
            total_chapters=10,
        )
        workspace_service.create(
            "ws-hb",
            display_name="x",
            genre="x",
            premise="x",
            style_template="heroic_battle",
            total_chapters=10,
        )
        workspace_service.create(
            "ws-scifi",
            display_name="x",
            genre="scifi",
            premise="x",
            total_chapters=10,
        )
        assert service._resolve_progression_consistency("ws-xianxia") == (
            True,
            "境界",
        )
        assert service._resolve_progression_consistency("ws-hb") == (False, "战力")
        assert service._resolve_progression_consistency("ws-scifi") == (
            False,
            "境界",
        )

    def test_orchestrate_passes_progression_to_orchestrate_chapter(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_orchestrate`` 把 ``_resolve_progression_consistency`` 的 (enabled,label)
        透传 orchestrate_chapter(progression_consistency_enabled=/progression_label=)。"""
        monkeypatch.setattr(
            chapter_service,
            "_resolve_progression_consistency",
            lambda _wid: (True, "战力"),
        )
        captured: dict[str, object] = {}

        def _capture_orchestrate(
            *,
            chapter_card: ChapterCard,
            progression_consistency_enabled: bool = False,
            progression_label: str = "境界",
            **_kwargs: object,
        ):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            captured["enabled"] = progression_consistency_enabled
            captured["label"] = progression_label
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )
        chapter_service.write(prepared_workspace, "ch-0002")
        assert captured["enabled"] is True
        assert captured["label"] == "战力"


class TestEscalationDetailsSync:
    """v1.1 acceptance 修复2 / Fix D:``chapters.escalation_details``
    落库 + 透出 round-trip(``_sync_escalation_details_safely`` /
    ``_read_escalation_details`` / ``status()`` 字段)。"""

    @staticmethod
    def _build_service_with_db(
        workspace_service: WorkspaceService, mock_client: MagicMock
    ) -> tuple[ChapterService, dict[str, object]]:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.core.db import Base

        engines: dict[str, object] = {}

        def _factory(workspace_id: str) -> _Session:
            from src.core.workspace.workspace_paths import WorkspacePaths

            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engines.get(workspace_id)
            if engine is None:
                engine = create_engine(f"sqlite:///{paths.db_path}")
                Base.metadata.create_all(engine)  # type: ignore[arg-type]
                engines[workspace_id] = engine
            return _Session(engine)  # type: ignore[arg-type]

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
            session_factory=_factory,
        )
        return service, engines

    def test_sync_write_read_and_status_roundtrip(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        service, _ = self._build_service_with_db(workspace_service, mock_client)
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        # 先用 upsert 建行(human_intervention 形态)
        service._upsert_chapter_state("ws-demo", "ch-0007", state="human_intervention")

        details = {
            "reason": "must_infeasible_pre_write",
            "checked_at_state": "pre_write",
            "findings": [
                {
                    "must_text": "沈舟在防空洞强行压制副人格救下流浪汉",
                    "confidence": 0.95,
                    "evidence": "ch-0006 已完结收束写明该冲突已解决。",
                    "suggestion": "建议从卡中删除该 must。",
                }
            ],
        }
        service._sync_escalation_details_safely("ws-demo", "ch-0007", details=details)
        assert service._read_escalation_details("ws-demo", "ch-0007") == details

        # status() 透出
        info = service.status("ws-demo", "ch-0007")
        assert info.escalation_details == details
        assert info.escalation_details["findings"][0]["suggestion"].startswith(
            "建议从卡中删除"
        )

    def test_zero_version_integrity_stop_can_explicitly_write_again(
        self, workspace_service: WorkspaceService, mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.orchestrator.chapter_orchestrator import OrchestratorResult

        service, _ = self._build_service_with_db(workspace_service, mock_client)
        workspace_service.create("ws-demo", display_name="x", genre="scifi", premise="y")
        service.save_chapter_card("ws-demo", _make_card("ch-0001", chapter_number=1))
        service._upsert_chapter_state("ws-demo", "ch-0001", state="human_intervention")
        details = {
            "reason": "writer_draft_integrity_failed", "checked_at_state": "post_writer_pre_version",
            "reasons": ["macro_repetition"], "details": {"actual": 3200},
        }
        service._sync_escalation_details_safely("ws-demo", "ch-0001", details=details)
        info = service.status("ws-demo", "ch-0001")
        assert info.escalation_details == details
        assert info.version_count == 0
        stopped = OrchestratorResult(
            final_state=ChapterState.HUMAN_INTERVENTION, escalation_reason="writer_draft_integrity_failed",
            versions=[], reviews=[], final_path=None, final_version_id=None,
            revise_count=0, rewrite_count=0,
        )
        capture = MagicMock(return_value=stopped)
        monkeypatch.setattr(service, "_orchestrate", capture)
        # 模拟下一次用户显式点击既有 Write；本测试不发送 LLM 请求。
        service.write("ws-demo", "ch-0001")
        assert capture.call_args.kwargs["initial_state"] == ChapterState.DRAFTING
        assert capture.call_args.kwargs["initial_rewrite_count"] == 0
        mock_client.complete.assert_not_called()

    def test_sync_clears_stale_details_on_none(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """details=None 清旧值 — 防上一轮 Fix D 详情在新一轮(budget 耗尽 /
        正常通过)后残留误导 UI。"""
        service, _ = self._build_service_with_db(workspace_service, mock_client)
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        service._upsert_chapter_state("ws-demo", "ch-0007", state="human_intervention")
        service._sync_escalation_details_safely(
            "ws-demo", "ch-0007", details={"reason": "must_infeasible_pre_write"}
        )
        assert service._read_escalation_details("ws-demo", "ch-0007") is not None
        service._sync_escalation_details_safely("ws-demo", "ch-0007", details=None)
        assert service._read_escalation_details("ws-demo", "ch-0007") is None

    def test_read_returns_none_without_row(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        service, _ = self._build_service_with_db(workspace_service, mock_client)
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        assert service._read_escalation_details("ws-demo", "ch-0001") is None


class TestStyleAnchorBookWiringW2S2:
    """v2 w2-s2 注入端接线防回退:ChapterService 解析 style_anchor_source
    门控 + 透传 orchestrate_chapter(style_anchor_book=)。

    防未来改 ``_orchestrate`` / ``_resolve_style_anchor_book`` 断了解析或
    透传 → A/B ON 臂在生产中静默退化为前章锚而 mock 机制测照不出。
    """

    def test_resolve_style_anchor_book_reads_generation_config(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """无字段(引擎默认 prev_chapter,v2 间隙回退)→ False;显式 book_anchor
        (工作区打开)→ True;读失败(不存在工作区)→ False(默认)。"""
        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
        )
        # 默认(无 project.yaml / 无 generation 段 → 引擎默认 prev_chapter)→ False
        assert service._resolve_style_anchor_book(prepared_workspace) is False
        # 显式 book_anchor(工作区打开注入端)→ True
        import yaml as _yaml

        project_yaml = (
            workspace_service.workspaces_root / prepared_workspace / "project.yaml"
        )
        project_yaml.write_text(
            _yaml.safe_dump(
                {"generation": {"style_anchor_source": "book_anchor"}},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        assert service._resolve_style_anchor_book(prepared_workspace) is True
        # 不存在的工作区 → load_generation_config 优雅返引擎默认(prev_chapter)→ False
        # (该路径不抛异常,故走默认而非 except 兜底;except 兜底由真 yaml 语法错触发)
        assert service._resolve_style_anchor_book("ws-missing-xyz") is False

    def test_orchestrate_passes_style_anchor_book_to_orchestrate_chapter(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            chapter_service,
            "_resolve_style_anchor_book",
            lambda _wid: True,
        )
        captured: dict[str, object] = {}

        def _capture_orchestrate(
            *,
            chapter_card: ChapterCard,
            style_anchor_book: bool = False,
            **_kwargs: object,
        ):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            captured["style_anchor_book"] = style_anchor_book
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )
        chapter_service.write(prepared_workspace, "ch-0002")
        assert captured["style_anchor_book"] is True

    def test_resolve_anti_tic_trim_reads_generation_config(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """v2 W3.5 wave-3 + 校准联动三态:override=None + router=None(无校准)→ 默认
        False;工作区显式 True → True(覆盖,不看校准);读失败(不存在工作区)→ False
        (零回归)。校准驱动(None → 模型校准默认)完整测见 test_resolve_trim_calibration_tristate。"""
        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
        )
        # override 未设 + router=None(无校准上下文)→ 默认关 False
        assert service._resolve_anti_tic_trim(prepared_workspace, None) is False
        # 工作区显式打开 → True(三态:显式覆盖优先,与校准无关)
        import yaml as _yaml

        project_yaml = (
            workspace_service.workspaces_root / prepared_workspace / "project.yaml"
        )
        project_yaml.write_text(
            _yaml.safe_dump(
                {"generation": {"anti_tic_trim_enabled": True}},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        assert service._resolve_anti_tic_trim(prepared_workspace, None) is True
        # 不存在的工作区 → override 读空 + router=None → False
        assert service._resolve_anti_tic_trim("ws-missing-xyz", None) is False

    def test_orchestrate_passes_anti_tic_trim_to_orchestrate_chapter(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """防回退:_orchestrate 把 _resolve_anti_tic_trim 结果透传 orchestrate_chapter。
        防未来改 _orchestrate / _resolve_anti_tic_trim 断了解析或透传 → ON 在生产中
        静默失效(开关开了但 trim 不跑)而 mock 机制测照不出。"""
        monkeypatch.setattr(
            chapter_service,
            "_resolve_anti_tic_trim",
            lambda _wid, _router: True,
        )
        captured: dict[str, object] = {}

        def _capture_orchestrate(
            *,
            chapter_card: ChapterCard,
            anti_tic_trim_enabled: bool = False,
            **_kwargs: object,
        ):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            captured["anti_tic_trim_enabled"] = anti_tic_trim_enabled
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )
        chapter_service.write(prepared_workspace, "ch-0002")
        assert captured["anti_tic_trim_enabled"] is True

    def test_resolve_word_count_trim_reads_generation_config(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
    ) -> None:
        """v2 W3.5-s3rev + 校准联动三态:override=None + router=None(无校准)→ 默认
        False;工作区显式 True → True(覆盖,不看校准);读失败(不存在工作区)→ False
        (零回归)。校准驱动(None → 模型校准默认)完整测见 test_resolve_trim_calibration_tristate。"""
        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
        )
        assert service._resolve_word_count_trim(prepared_workspace, None) is False
        import yaml as _yaml

        project_yaml = (
            workspace_service.workspaces_root / prepared_workspace / "project.yaml"
        )
        project_yaml.write_text(
            _yaml.safe_dump(
                {"generation": {"word_count_trim_enabled": True}},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        assert service._resolve_word_count_trim(prepared_workspace, None) is True
        assert service._resolve_word_count_trim("ws-missing-xyz", None) is False

    def test_orchestrate_passes_word_count_trim_to_orchestrate_chapter(
        self,
        chapter_service: ChapterService,
        mock_client: MagicMock,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """防回退:_orchestrate 把 _resolve_word_count_trim 结果透传 orchestrate_chapter。
        防未来改断了解析或透传 → ON 在生产中静默失效(开关开了但 trim 不跑)。"""
        monkeypatch.setattr(
            chapter_service,
            "_resolve_word_count_trim",
            lambda _wid, _router: True,
        )
        captured: dict[str, object] = {}

        def _capture_orchestrate(
            *,
            chapter_card: ChapterCard,
            word_count_trim_enabled: bool = False,
            **_kwargs: object,
        ):
            from src.core.orchestrator.chapter_orchestrator import (
                OrchestratorResult,
            )

            captured["word_count_trim_enabled"] = word_count_trim_enabled
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter",
            _capture_orchestrate,
        )
        chapter_service.write(prepared_workspace, "ch-0002")
        assert captured["word_count_trim_enabled"] is True

    def test_resolve_trim_calibration_tristate(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v2 W3.5 校准联动:_resolve_* 的 None 态(无工作区 override)→ 走当前 writer
        模型校准默认。stub router(writer=deepseek-v4-pro)+ deepseek 校准(overshoot/high)
        → word_count / anti_tic 都 True;router=None(无校准上下文)→ False(零回归对比)。
        显式 override 优先见 test_resolve_*_reads_generation_config。"""
        # deepseek 校准 config(只需 model_calibration 段;_calibration_default_trim 走
        # settings.config_yaml_path load calibration)。
        calib_yaml = (
            "model_calibration:\n"
            "  deepseek-v4-pro:\n"
            '    calibrated_at: "2026-06-16"\n'
            '    calibration_corpus: "test"\n'
            "    global:\n"
            "      length_tendency: overshoot\n"
            "      anti_stamp_strength: high\n"
            "      recommended_temperature: 0.85\n"
            "      thinking_default: false\n"
            "      reviewer_threshold_adjustment: 0.0\n"
            "    per_genre:\n"
            "      introspection:\n"
            "        chars_per_beat: 1534\n"
            "        chars_per_chapter: 4602\n"
            "        length_compliance: 0.667\n"
            "        content_units_per_chapter: 3\n"
            "        sample_chapters: 6\n"
        )
        cfg = tmp_path / "ds_calib.yaml"
        cfg.write_text(calib_yaml, encoding="utf-8")
        from src.config import settings as _settings

        monkeypatch.setattr(_settings, "config_yaml_path", cfg)

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="claude-sonnet-4-6",
        )

        class _StubProfile:
            model = "deepseek-v4-pro"

        class _StubRouter:
            def select_profile(self, _agent_name: str) -> "_StubProfile":
                return _StubProfile()

        router = _StubRouter()
        # 无工作区 override + writer=deepseek 校准(overshoot/high)→ None 态走校准 → 都开
        assert service._resolve_word_count_trim(prepared_workspace, router) is True
        assert service._resolve_anti_tic_trim(prepared_workspace, router) is True
        # router=None(无校准上下文,如 legacy / 测试)→ 默认关(零回归对比)
        assert service._resolve_word_count_trim(prepared_workspace, None) is False
        assert service._resolve_anti_tic_trim(prepared_workspace, None) is False
