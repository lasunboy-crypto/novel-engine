"""W3.5-R4 同步 ledger、ready、卡批准与 Writer stale 的产品路径回归。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.agents.chapter_summary_extractor import (
    continuity_ledger_provenance,
    extract_and_persist_continuity_ledger,
)
from src.core.db import Base
from src.core.db.models import (
    ChapterCardORM,
    ChapterORM,
    ChapterSummaryORM,
    ChapterVersionORM,
)
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.continuity_ledger import (
    ContinuityLedgerProvenance,
    sha256_text,
)
from src.core.orchestrator.chapter_orchestrator import OrchestratorResult
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_card_service import (
    CardContinuityBridgeRequiredError,
    CardContinuityLedgerStaleError,
    ChapterCardService,
)
from src.services.chapter_service import (
    ChapterCardContinuityBridgeError,
    ChapterCardContinuityStaleError,
    ChapterService,
)
from src.services.workspace_service import WorkspaceService


_WORKSPACE_ID = "ws-r4-product"
_IMMEDIATE_ACTION = (
    "低频的嗡鸣声还在继续，镜面里的画面开始扭曲、渗血，"
    "仿佛有什么东西正试图从那个第一视角的死亡瞬间里爬出来。"
)
_FINAL_TEXT = f"旅馆镜厅内，林澈死死盯着青铜镜。{_IMMEDIATE_ACTION}"
_R8_LEGACY_TAIL = (
    "林澈没有朝他们走去，只把整座小镇是否为活人陵墓留作推测。"
    "而他刚刚推开的那扇酒馆门，不过是其中一块尚未被风沙完全掩埋的墓碑。"
)
_R8_LEGACY_RULE_ACTION = (
    "而他刚刚推开的那扇酒馆门，不过是其中一块尚未被风沙完全掩埋的墓碑。"
)
_R8_LEDGER_ACTIONS = [
    "林澈只观察镇民而未靠近，对活人陵墓的判断仍是推测",
    "林澈没有放弃调查恩师，下一步行动方向仍悬置",
]
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class _LedgerLLM(LLMClient):
    """返回逐条引用 strict source catalog ID 的受控摘要结果。"""

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        **gen_params: Any,
    ) -> LLMResponse:
        payload = {
            "summary_text": "青铜镜异变仍在继续，林澈面对未解除的即时危机。",
            "pov_character": "lin-che",
            "pov_evidence_source_id": "E0001",
            "scene_setting": "旅馆镜厅",
            "scene_setting_evidence_source_id": "E0001",
            "key_events": [
                {
                    "description": "青铜镜开始扭曲渗血",
                    "actors": ["lin-che"],
                    "location": "旅馆镜厅",
                    "evidence_source_id": "E0002",
                }
            ],
            "character_interactions": [],
            "unfinished_actions": [
                {
                    "description": _IMMEDIATE_ACTION,
                    "is_immediate": True,
                    "evidence_source_id": "E0002",
                }
            ],
            "character_states": [
                {
                    "character_id": "lin-che",
                    "state": "正面对镜面异变",
                    "evidence_source_id": "E0001",
                }
            ],
            "established_facts": [
                {
                    "fact": "青铜镜已发生渗血异变",
                    "evidence_source_id": "E0002",
                }
            ],
            "revealed_mechanics": [
                {
                    "mechanic": "镜面异变可投射死亡瞬间",
                    "evidence_source_id": "E0002",
                }
            ],
        }
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            model_used=f"fake-{model or 'default'}",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            finish_reason="end_turn",
        )


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    service = WorkspaceService(
        workspaces_root=workspaces_root,
        archives_root=archives_root,
    )
    service.create(
        _WORKSPACE_ID,
        display_name="R4 产品路径",
        genre="悬疑",
        premise="验证连续性硬水位",
        total_chapters=2,
        activate=False,
    )
    return service


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def factory(_workspace_id: str) -> Session:
        return Session(engine)

    return factory


def _card(
    chapter_id: str,
    chapter_number: int,
    *,
    provenance=None,
    bridge: str = "",
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=_WORKSPACE_ID,
        volume_id="v1",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章",
        tension_level=7,
        word_count_min=2500,
        word_count_max=4500,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="lin-che",
        setting="旅馆",
        continuity_context=ContinuityContext(
            prev_chapter_tail=_FINAL_TEXT if chapter_number > 1 else "",
            unfinished_actions=([_IMMEDIATE_ACTION] if chapter_number > 1 else []),
            time_gap="数小时后" if chapter_number > 1 else "开篇",
            scene_continuation="switch",
            continuity_bridge=bridge,
            pov_change=False,
            pov_from="lin-che" if chapter_number > 1 else None,
            pov_to="lin-che",
        ),
        next_chapter_seed="继续追查",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["lin-che"],
        card_status="pending_review",
        continuity_ledger_provenance=provenance,
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _stub_projection_recovery_case(
    *,
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    monkeypatch: pytest.MonkeyPatch,
    continuity_bridge: str = "已承接上一章事项后再换景。",
    evidence_records: list[dict[str, Any]] | None = None,
) -> tuple[ChapterService, WorkspacePaths, ChapterCard]:
    """构造 provenance 未变、仅命中 R8 legacy rule 污染签名的产品卡。"""

    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=_LedgerLLM(),
        model="fake-ledger",
        session_factory=session_factory,
        memory_enrichment_scheduler=lambda _task: None,
    )
    paths = WorkspacePaths(
        _WORKSPACE_ID,
        workspaces_root=workspace_service.workspaces_root,
    )
    provenance = ContinuityLedgerProvenance(
        chapter_id="ch-0001",
        source_version_id=1,
        source_text_hash="a" * 64,
        ledger_hash="b" * 64,
    )
    legacy_context = ContinuityContext(
        prev_chapter_tail=_R8_LEGACY_TAIL,
        unfinished_actions=[_R8_LEGACY_RULE_ACTION],
        time_gap="数小时后",
        scene_continuation="switch",
        continuity_bridge=continuity_bridge,
        pov_change=False,
        pov_from="lin-che",
        pov_to="lin-che",
    )
    legacy_refreshed_card = _card(
        "ch-0002",
        2,
        provenance=provenance,
        bridge=continuity_bridge,
    ).model_copy(
        update={
            "card_status": "approved",
            "continuity_context": legacy_context,
        }
    )
    chapter_service.save_chapter_card(_WORKSPACE_ID, legacy_refreshed_card)

    ledger = type(
        "ValidatedLedger",
        (),
        {
            "raw_final_tail": _R8_LEGACY_TAIL,
            "unfinished_actions": list(_R8_LEDGER_ACTIONS),
            "pov_character": "lin-che",
            "evidence_records": list(evidence_records or []),
        },
    )()
    monkeypatch.setattr(
        chapter_service,
        "require_ready_for_next_chapter",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.services.chapter_service.load_validated_continuity_ledger",
        lambda **_kwargs: ledger,
    )
    monkeypatch.setattr(
        "src.services.chapter_service.continuity_ledger_provenance",
        lambda _ledger: provenance,
    )
    return chapter_service, paths, legacy_refreshed_card


def test_product_order_bridge_and_rewrite_stale(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
) -> None:
    llm = _LedgerLLM()
    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=llm,
        model="fake-ledger",
        session_factory=session_factory,
        memory_enrichment_scheduler=lambda _task: None,
    )
    card_service = ChapterCardService(
        workspace_service=workspace_service,
        chapter_service=chapter_service,
        session_factory=session_factory,
        client=llm,
        model="fake-ledger",
        ws_manager=None,
    )
    paths = WorkspacePaths(
        _WORKSPACE_ID,
        workspaces_root=workspace_service.workspaces_root,
    )

    first_card = _card("ch-0001", 1)
    chapter_service.save_chapter_card(_WORKSPACE_ID, first_card)
    final_path = paths.chapter_version_file("ch-0001", "final.md")
    version_path = paths.chapter_version_file("ch-0001", "v1.md")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(_FINAL_TEXT, encoding="utf-8")
    version_path.write_text(_FINAL_TEXT, encoding="utf-8")
    final_written_at = datetime.fromtimestamp(final_path.stat().st_mtime, UTC)

    chapter_service._upsert_chapter_state(
        _WORKSPACE_ID,
        "ch-0001",
        state="approved",
    )
    with session_factory(_WORKSPACE_ID) as session:
        chapter_row = session.get(ChapterORM, "ch-0001")
        assert chapter_row is not None
        chapter_row.current_version = 1
        session.add(
            ChapterVersionORM(
                chapter_id="ch-0001",
                version_id=1,
                content=_FINAL_TEXT,
                word_count=len(_FINAL_TEXT),
                created_at=datetime.now(UTC),
                creation_type="draft",
                model_used="fake-ledger",
                review_score=None,
                notes="",
                style_fingerprint=None,
            )
        )
        session.commit()
    ledger = extract_and_persist_continuity_ledger(
        workspace_id=_WORKSPACE_ID,
        chapter_id="ch-0001",
        source_version_id=1,
        paths=paths,
        session_factory=session_factory,
        llm_client=llm,
        llm_model="fake-ledger",
        prompts_dir=_PROMPTS_DIR,
    )
    provenance = continuity_ledger_provenance(ledger)
    second_card = _card("ch-0002", 2, provenance=provenance)
    card_service.save_uploaded_card(
        _WORKSPACE_ID,
        "ch-0002",
        second_card,
    )

    # 即使 ledger 已校验，ready 尚未落账时仍不能批准下一卡。
    with pytest.raises(CardContinuityLedgerStaleError, match="ready"):
        card_service.transition_card_status(
            _WORKSPACE_ID,
            "ch-0002",
            action="approve",
        )

    chapter_service._mark_ready_for_next_chapter(
        _WORKSPACE_ID,
        "ch-0001",
        paths=paths,
        final_path=final_path,
        final_version_id=1,
    )

    # F ch-0006 形状：即时危机直接跳数小时后且换景，没有 bridge 必须阻断。
    with pytest.raises(
        CardContinuityBridgeRequiredError, match="bridge"
    ) as bridge_exc:
        card_service.transition_card_status(
            _WORKSPACE_ID,
            "ch-0002",
            action="approve",
        )
    assert bridge_exc.value.required_actions == [_IMMEDIATE_ACTION]

    bridge = (
        f"先承接上一章即时危机：“{_IMMEDIATE_ACTION}”"
        "林澈确认异变被隔离后，才切到数小时后的旅馆外。"
    )
    bridged_card = _card(
        "ch-0002",
        2,
        provenance=provenance,
        bridge=bridge,
    )
    card_service.save_uploaded_card(
        _WORKSPACE_ID,
        "ch-0002",
        bridged_card,
    )
    card_service.transition_card_status(
        _WORKSPACE_ID,
        "ch-0002",
        action="approve",
    )

    with session_factory(_WORKSPACE_ID) as session:
        summary_row = session.get(ChapterSummaryORM, "ch-0001")
        chapter_row = session.get(ChapterORM, "ch-0001")
        card_row = session.get(ChapterCardORM, "ch-0002")
        assert summary_row is not None
        assert chapter_row is not None
        assert card_row is not None
        assert summary_row.validated_at is not None
        records = summary_row.evidence_records
        assert records
        assert {record["evidence_quote"] for record in records} == {
            "旅馆镜厅内，林澈死死盯着青铜镜。",
            _IMMEDIATE_ACTION,
        }
        assert all(
            record["evidence_quote"] in _FINAL_TEXT
            and record["evidence_fingerprint"] == sha256_text(record["evidence_quote"])
            for record in records
        )
        persisted_payload = json.dumps(
            {
                "key_events": summary_row.key_events,
                "character_interactions": summary_row.character_interactions,
                "character_states": summary_row.character_states,
                "evidence_records": records,
            },
            ensure_ascii=False,
        )
        assert "evidence_source_id" not in persisted_payload
        assert "E0001" not in persisted_payload
        assert "E0002" not in persisted_payload
        assert chapter_row.ready_for_next_chapter_at is not None
        assert card_row.card_approved_at is not None
        assert final_written_at <= _utc(summary_row.created_at)
        assert _utc(summary_row.validated_at) <= _utc(
            chapter_row.ready_for_next_chapter_at
        )
        assert _utc(chapter_row.ready_for_next_chapter_at) < _utc(
            card_row.card_approved_at
        )

    # B0：只有经过真实 final/version/SQLite/ready/source 全量复核的 ledger
    # 才能在首次 Writer 出网前形成瞬态 capsule。
    approved_card = chapter_service.read_chapter_card(_WORKSPACE_ID, "ch-0002")
    preflight = chapter_service._prepare_card_continuity_preflight(
        workspace_id=_WORKSPACE_ID,
        card=approved_card,
        paths=paths,
    )
    assert preflight.card == approved_card
    assert preflight.capsule is not None
    assert preflight.capsule.provenance == provenance
    assert list(preflight.capsule.unfinished_actions) == [_IMMEDIATE_ACTION]

    # 上一章正文改写后，已批准的旧下一卡在 Writer 前因来源 hash 失配阻断。
    rewritten = _FINAL_TEXT + "异变突然改变了方向。"
    final_path.write_text(rewritten, encoding="utf-8")
    version_path.write_text(rewritten, encoding="utf-8")
    with pytest.raises(ChapterCardContinuityStaleError, match="连续性小账本"):
        chapter_service._validate_card_continuity_preflight(
            workspace_id=_WORKSPACE_ID,
            card=bridged_card.model_copy(update={"card_status": "approved"}),
            paths=paths,
        )


def test_provenance_matched_projection_normalizes_and_skips_legacy_refresh(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 ch3：同源 ledger 只修确定性投影，随后禁止 legacy rule 二次覆盖。"""

    chapter_service, paths, legacy_refreshed_card = _stub_projection_recovery_case(
        workspace_service=workspace_service,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
    )

    repaired_card = chapter_service._validate_card_continuity_preflight(
        workspace_id=_WORKSPACE_ID,
        card=legacy_refreshed_card,
        paths=paths,
    )
    assert repaired_card.continuity_context.unfinished_actions == _R8_LEDGER_ACTIONS
    assert (
        chapter_service.read_chapter_card(_WORKSPACE_ID, "ch-0002")
        == legacy_refreshed_card
    )
    assert (
        chapter_service._validate_card_continuity_preflight(
            workspace_id=_WORKSPACE_ID,
            card=repaired_card,
            paths=paths,
        )
        == repaired_card
    )

    def _unexpected_legacy_refresh(**_kwargs: Any) -> ContinuityContext:
        raise AssertionError("provenanced card must skip legacy refresh")

    monkeypatch.setattr(
        "src.services.chapter_service.build_continuity_context",
        _unexpected_legacy_refresh,
    )
    assert (
        chapter_service._maybe_refresh_continuity(paths, repaired_card, None)
        == repaired_card
    )
    refresh_result = chapter_service.refresh_continuity_all(_WORKSPACE_ID)
    assert [(item.chapter_id, item.outcome) for item in refresh_result.items] == [
        ("ch-0002", "unchanged")
    ]


def test_orchestrate_uses_repaired_projection_without_persisting_card(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整 service 接线把内存恢复卡交给 orchestrator，旧 refresh 不运行。"""

    chapter_service, paths, legacy_card = _stub_projection_recovery_case(
        workspace_service=workspace_service,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
    )
    captured: dict[str, Any] = {}

    def _capture_orchestrate(
        *,
        chapter_card: ChapterCard,
        continuity_capsule: Any = None,
        **_kwargs: Any,
    ) -> OrchestratorResult:
        captured["card"] = chapter_card
        captured["continuity_capsule"] = continuity_capsule
        return OrchestratorResult(
            final_state=ChapterState.HUMAN_INTERVENTION,
            revise_count=2,
            rewrite_count=3,
            escalation_reason="test_terminal",
        )

    def _unexpected_legacy_refresh(**_kwargs: Any) -> ContinuityContext:
        raise AssertionError("provenanced card must skip legacy refresh")

    monkeypatch.setattr(
        "src.services.chapter_service.orchestrate_chapter",
        _capture_orchestrate,
    )
    monkeypatch.setattr(
        "src.services.chapter_service.build_continuity_context",
        _unexpected_legacy_refresh,
    )
    monkeypatch.setattr(
        "src.services.chapter_service.build_memory_section",
        lambda _paths: "",
    )
    monkeypatch.setattr(
        chapter_service,
        "_outline_contract_required",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        chapter_service,
        "_load_world_contract",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        chapter_service,
        "_upsert_chapter_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        chapter_service,
        "_wrap_state_transition_with_db",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        chapter_service,
        "_resolve_progression_consistency",
        lambda _workspace_id: (False, "境界"),
    )
    monkeypatch.setattr(
        chapter_service,
        "_resolve_style_anchor_book",
        lambda _workspace_id: False,
    )
    monkeypatch.setattr(
        chapter_service,
        "_resolve_anti_tic_trim",
        lambda _workspace_id, _router: False,
    )
    monkeypatch.setattr(
        chapter_service,
        "_resolve_word_count_trim",
        lambda _workspace_id, _router: False,
    )
    monkeypatch.setattr(
        chapter_service,
        "_sync_escalation_details_safely",
        lambda *_args, **_kwargs: None,
    )

    result = chapter_service._orchestrate(
        workspace_id=_WORKSPACE_ID,
        chapter_id="ch-0002",
        initial_state=ChapterState.REWRITING,
        initial_revise_count=2,
        initial_rewrite_count=3,
    )

    assert result.final_state == ChapterState.HUMAN_INTERVENTION
    assert captured["card"].continuity_context.unfinished_actions == (
        _R8_LEDGER_ACTIONS
    )
    # 本 legacy 恢复 stub 只提供旧投影字段、没有完整 ledger coverage；B0 必须
    # 在首次出网前关闭短版并交付原完整 Prompt，不能补猜或 Provider retry。
    assert captured["continuity_capsule"] is None
    assert chapter_service.read_chapter_card(_WORKSPACE_ID, "ch-0002") == legacy_card
    assert paths.cards_dir.joinpath("ch-0002.json").is_file()


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("prev_chapter_tail", "未知来源改写的尾文"),
        ("unfinished_actions", ["未知来源改写的未完动作"]),
        ("pov_from", "chu-yao"),
    ],
)
def test_non_legacy_projection_mismatch_remains_fail_closed(
    field: str,
    bad_value: Any,
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知 tail/actions/POV 漂移不得借 R8 窄恢复例外被静默覆盖。"""

    chapter_service, paths, legacy_card = _stub_projection_recovery_case(
        workspace_service=workspace_service,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
    )
    bad_context = legacy_card.continuity_context.model_copy(update={field: bad_value})
    bad_card = legacy_card.model_copy(update={"continuity_context": bad_context})
    chapter_service.save_chapter_card(_WORKSPACE_ID, bad_card)
    card_path = chapter_service.card_path_for(_WORKSPACE_ID, "ch-0002")
    before = card_path.read_text(encoding="utf-8")

    with pytest.raises(ChapterCardContinuityStaleError):
        chapter_service._validate_card_continuity_preflight(
            workspace_id=_WORKSPACE_ID,
            card=bad_card,
            paths=paths,
        )

    assert card_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("mode", ["missing", "stale"])
def test_missing_or_stale_provenance_never_uses_legacy_repair(
    mode: str,
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 provenance 或来源 hash 漂移继续遵守 R4 fail-closed 合同。"""

    chapter_service, paths, legacy_card = _stub_projection_recovery_case(
        workspace_service=workspace_service,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
    )
    frozen = legacy_card.continuity_ledger_provenance
    assert frozen is not None
    replacement = (
        None
        if mode == "missing"
        else frozen.model_copy(update={"ledger_hash": "c" * 64})
    )
    bad_card = legacy_card.model_copy(
        update={"continuity_ledger_provenance": replacement}
    )
    chapter_service.save_chapter_card(_WORKSPACE_ID, bad_card)
    card_path = chapter_service.card_path_for(_WORKSPACE_ID, "ch-0002")
    before = card_path.read_text(encoding="utf-8")

    with pytest.raises(ChapterCardContinuityStaleError):
        chapter_service._validate_card_continuity_preflight(
            workspace_id=_WORKSPACE_ID,
            card=bad_card,
            paths=paths,
        )

    assert card_path.read_text(encoding="utf-8") == before


def test_legacy_projection_repair_bridge_failure_does_not_persist(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """精确旧污染即使可识别，bridge 不合格仍先拦截且不落修复。"""

    immediate_action = _R8_LEDGER_ACTIONS[0]
    chapter_service, paths, legacy_card = _stub_projection_recovery_case(
        workspace_service=workspace_service,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
        continuity_bridge="",
        evidence_records=[
            {
                "field": "unfinished_actions",
                "index": 0,
                "fact_text": immediate_action,
                "evidence_quote": "只观察镇民而未靠近",
                "evidence_fingerprint": sha256_text("只观察镇民而未靠近"),
                "is_immediate": True,
            }
        ],
    )
    card_path = chapter_service.card_path_for(_WORKSPACE_ID, "ch-0002")
    before = card_path.read_text(encoding="utf-8")

    with pytest.raises(ChapterCardContinuityBridgeError):
        chapter_service._validate_card_continuity_preflight(
            workspace_id=_WORKSPACE_ID,
            card=legacy_card,
            paths=paths,
        )

    assert card_path.read_text(encoding="utf-8") == before


def test_legacy_projection_repair_detects_concurrent_card_change(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复卡交给 orchestrator 前发现同卡变化时保留原文件并硬停。"""

    chapter_service, paths, legacy_card = _stub_projection_recovery_case(
        workspace_service=workspace_service,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
    )
    card_path = chapter_service.card_path_for(_WORKSPACE_ID, "ch-0002")
    before = card_path.read_text(encoding="utf-8")
    concurrent_card = legacy_card.model_copy(update={"title": "并发请求改写后的标题"})
    monkeypatch.setattr(
        chapter_service,
        "read_chapter_card",
        lambda *_args, **_kwargs: concurrent_card,
    )

    with pytest.raises(ChapterCardContinuityStaleError, match="并发变化"):
        chapter_service._validate_card_continuity_preflight(
            workspace_id=_WORKSPACE_ID,
            card=legacy_card,
            paths=paths,
        )

    assert card_path.read_text(encoding="utf-8") == before


def test_provenanced_card_without_session_factory_fails_closed(
    workspace_service: WorkspaceService,
) -> None:
    """带 ledger 身份的卡不能在缺少 DB 验证能力时跳过 preflight。"""

    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=_LedgerLLM(),
        model="fake-ledger",
        session_factory=None,
        memory_enrichment_scheduler=lambda _task: None,
    )
    paths = WorkspacePaths(
        _WORKSPACE_ID,
        workspaces_root=workspace_service.workspaces_root,
    )
    provenance = ContinuityLedgerProvenance(
        chapter_id="ch-0001",
        source_version_id=1,
        source_text_hash="a" * 64,
        ledger_hash="b" * 64,
    )
    card = _card("ch-0002", 2, provenance=provenance).model_copy(
        update={"card_status": "approved"}
    )

    with pytest.raises(ChapterCardContinuityStaleError, match="session_factory"):
        chapter_service._validate_card_continuity_preflight(
            workspace_id=_WORKSPACE_ID,
            card=card,
            paths=paths,
        )
