"""章节路由集成用例(P1-B-19)。

覆盖 10 个端点(9 个章节 + 1 个 tasks)的"主路径 + 错误路径",并验证 P1-B-17
全局异常处理 + P1-B-18 双轨划分(路由前置守卫 vs service 异常翻译)在章节
路由的应用。

测试基础设施
============

复用 P1-B-15 ChapterService 测试套路:
- ``mock_client = MagicMock(spec=LLMClient)`` 注入 ``ChapterService``,
  Writer / Reviewer / Reviser agent + Orchestrator + ChapterVersioning 全真跑
- 通过 ``app.dependency_overrides[get_chapter_service] = lambda: ...`` 注入带
  mock 客户端的 service(**只 override 顶层 dep**,FastAPI 解依赖时一旦命中
  override 就不再继续解嵌套依赖)
- ``settings.prompts_dir`` monkeypatch 到 tmp 假模板目录;``load_prompt_template``
  LRU 每用例清
- ``task_registry.clear()`` 每用例清一次,避免 LRU 跨用例污染

错误码覆盖矩阵
==============

| 错误码 | 来源 |
|--------|------|
| MISSING_WORKSPACE_HEADER (422) | 路由前置依赖(get_workspace_id) |
| CHAPTER_CARD_NOT_FOUND (404) | 路由前置守卫(_ensure_card_exists) |
| CHAPTER_ID_MISMATCH (422) | 路由前置守卫(PUT /card body 与 URL) |
| CHAPTER_INVALID_STATE (409) | service 异常翻译(ChapterServiceError 兜底) |
| CHAPTER_VERSION_CONFLICT (409) | service 异常翻译(ChapterVersioningError) |
| WORKSPACE_CONFLICT (409) | service 异常翻译(WorkspaceServiceError) |
| TASK_NOT_FOUND (404) | tasks 路由(LRU 未命中) |
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api import task_registry
from src.api.routes import chapters as chapter_routes
from src.api.deps import get_chapter_service
from src.api.main import create_app
from src.config import settings
from src.core.agents import _prompt as prompt_module
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.services.chapter_service import ChapterService
from src.services.workspace_service import WorkspaceService
from tests.revision_contract_helpers import (
    contract_response_sequence,
    single_target_localized_response,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def workspaces_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture()
def archives_root(tmp_path: Path) -> Path:
    return tmp_path / "archives"


@pytest.fixture()
def workspace_service(workspaces_root: Path, archives_root: Path) -> WorkspaceService:
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture()
def mock_client() -> MagicMock:
    return MagicMock(spec=LLMClient)


@pytest.fixture()
def chapter_service(
    workspace_service: WorkspaceService, mock_client: MagicMock
) -> ChapterService:
    return ChapterService(
        workspace_service=workspace_service,
        client=mock_client,
        model="claude-sonnet-4-6",
    )


@pytest.fixture()
def fake_prompts_dir(tmp_path: Path) -> Path:
    """tmp_path 下建假模板目录,Writer / Reviewer / Reviser 三个 agent 共享。"""
    p = tmp_path / "prompts_root"
    p.mkdir()
    (p / "writer.md").write_text("FAKE_WRITER", encoding="utf-8")
    (p / "reviewer.md").write_text("FAKE_REVIEWER", encoding="utf-8")
    (p / "reviser.md").write_text("FAKE_REVISER", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _patch_prompts_dir(monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path) -> None:
    monkeypatch.setattr(settings, "prompts_dir", fake_prompts_dir)


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> Iterator[None]:
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _clear_task_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


@pytest.fixture()
def client(chapter_service: ChapterService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_chapter_service] = lambda: chapter_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- 工厂 -------------------------------------------------------------------


def _continuity(**overrides: object) -> ContinuityContext:
    payload: dict = dict(
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


def _card(chapter_id: str = "ch-0002", **overrides: object) -> ChapterCard:
    payload: dict = dict(
        chapter_id=chapter_id,
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章·破阵",
        tension_level=8,
        # P4-B-06:validator 默认放宽,本文件测 API 路由,validator 不参与。
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=3,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_continuity(),
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


def _writer_resp(content: str = "正文(略)") -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=1000, output_tokens=2000),
        finish_reason="end_turn",
    )


def _reviewer_resp(
    *,
    score: float = 8.0,
    hard_constraints_passed: bool = True,
    paragraph_anchor: str = "他握紧剑柄,目光不离对面那道身影",
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
        "issues": [
            {
                "paragraph_anchor": paragraph_anchor,
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


@pytest.fixture()
def prepared(
    workspace_service: WorkspaceService, chapter_service: ChapterService
) -> str:
    """创建 ws-demo 工作区 + 落 ch-0002 章节卡;返回 workspace_id。"""
    workspace_service.create(
        "ws-demo",
        display_name="科幻测试本",
        genre="scifi",
        premise="人类在月球建殖民地",
        total_chapters=120,
    )
    chapter_service.save_chapter_card("ws-demo", _card())
    return "ws-demo"


def _hdr(workspace_id: str = "ws-demo") -> dict[str, str]:
    return {"X-Workspace-Id": workspace_id}


def _assert_async_task_done(
    resp,
    *,
    expected_type: str,
):
    """v1.0.x Commit B 改造:6 端点全改 BackgroundTasks 模式,统一断言 helper。

    - 响应 202 Accepted + TaskInfo(status=pending)
    - TestClient 同步等 BackgroundTasks 跑完(starlette 实现细节,生产为真异步
      threadpool;测试同步等是 TestClient 特性,见 starlette 文档)
    - 读 task_registry.get(task_id) 拿最终 done 状态 + result dict

    返回 (info_dict, result_dict),让原测试断言 result 字段保持原样。
    """
    assert resp.status_code == 202, (
        f"期望 202 Accepted,实得 {resp.status_code}:{resp.text[:200]}"
    )
    body = resp.json()
    assert "data" in body and "error" not in body
    info = body["data"]
    assert info["type"] == expected_type
    assert info["status"] == "pending"
    assert info["progress"] == 0
    assert info["task_id"].startswith("task_")
    task_id = info["task_id"]
    # BackgroundTasks 已同步跑完(TestClient 行为),读 registry 验最终状态
    final = task_registry.get(task_id)
    assert final is not None, f"task_id={task_id} 未在 registry"
    assert final.status == "done", (
        f"期望 status=done,实得 {final.status} error={final.error}"
    )
    assert final.progress == 100
    return info, final.result


def _assert_async_task_failed(
    resp,
    *,
    expected_type: str,
    expected_code: str,
):
    """v1.0.x Commit B 改造:断言异步任务 → failed + error.code 命中。

    用于 ApiError 类异常(ChapterCardNotFound / ChapterInvalidState 等)
    现在从同步 HTTP 4xx 改成 task_registry update_to_failed + WS publish failed。
    """
    assert resp.status_code == 202, (
        f"期望 202 Accepted(失败也先 202 + 后台跑出 failed),"
        f"实得 {resp.status_code}:{resp.text[:200]}"
    )
    body = resp.json()
    info = body["data"]
    assert info["type"] == expected_type
    task_id = info["task_id"]
    final = task_registry.get(task_id)
    assert final is not None
    assert final.status == "failed", (
        f"期望 status=failed,实得 {final.status} result={final.result}"
    )
    assert final.error is not None
    assert final.error["code"] == expected_code, (
        f"期望 error.code={expected_code},实得 {final.error.get('code')}"
    )
    return info, final.error


class TestChapterTaskDurableCompletion:
    """UI ``done`` 必须晚于章节持久租约的精确完成。"""

    @staticmethod
    def _write_result() -> SimpleNamespace:
        return SimpleNamespace(
            final_state=SimpleNamespace(value="approved"),
            final_version_id=1,
            final_path=None,
            versions=[object()],
            revise_count=0,
            rewrite_count=0,
            escalation_reason=None,
            reviews=[],
        )

    @staticmethod
    def _revise_result() -> SimpleNamespace:
        return SimpleNamespace(
            version_id=2,
            word_count=100,
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
            creation_type="revise",
            model_used="mock-model",
            review_score=None,
            notes="test",
        )

    @pytest.mark.parametrize(
        (
            "runner_name",
            "service_method",
            "task_type",
            "result_kind",
            "kwargs",
        ),
        [
            (
                "_run_chapter_write_task",
                "write",
                "chapter_write",
                "write",
                {"profile_override": None},
            ),
            (
                "_run_chapter_rewrite_task",
                "rewrite",
                "chapter_rewrite",
                "write",
                {"prior_rewrite_count": 0, "profile_override": None},
            ),
            (
                "_run_chapter_re_review_task",
                "re_review",
                "chapter_re_review",
                "write",
                {"profile_override": None},
            ),
            (
                "_run_chapter_revise_task",
                "revise",
                "chapter_revise",
                "revise",
                {"issues": [], "source_version_id": 1},
            ),
        ],
    )
    def test_lease_completion_failure_never_reports_done(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runner_name: str,
        service_method: str,
        task_type: str,
        result_kind: str,
        kwargs: dict[str, object],
    ) -> None:
        service = MagicMock(spec=ChapterService)
        result = (
            self._write_result()
            if result_kind == "write"
            else self._revise_result()
        )
        getattr(service, service_method).return_value = result
        service.complete_operation_task.side_effect = RuntimeError(
            "durable lease release failed"
        )
        task_id = f"task-{service_method}-lease-failure"
        task_registry.record_pending(
            task_id,
            type=task_type,
            workspace_id="ws-demo",
        )
        events: list[str] = []
        monkeypatch.setattr(
            chapter_routes,
            "_publish_task_event",
            lambda _task_id, event, _data: events.append(event),
        )

        runner = getattr(chapter_routes, runner_name)
        runner(service, "ws-demo", "ch-0003", task_id, **kwargs)

        task = task_registry.get(task_id)
        assert task is not None
        assert task.status == "failed"
        assert task.result is None
        assert task.error is not None
        assert task.error["code"] == "INTERNAL_ERROR"
        assert events == ["stage", "failed"]
        service.complete_operation_task.assert_called_once_with(
            "ws-demo",
            "ch-0003",
            task_id=task_id,
        )
        service.release_operation_task_if_never_dispatched.assert_not_called()

    def test_done_is_published_only_after_lease_completion(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = MagicMock(spec=ChapterService)
        order: list[str] = []
        service.complete_operation_task.side_effect = lambda *_args, **_kwargs: (
            order.append("lease_complete")
        )
        monkeypatch.setattr(
            task_registry,
            "update_to_done",
            lambda *_args, **_kwargs: order.append("task_done"),
        )
        monkeypatch.setattr(
            chapter_routes,
            "_publish_task_event",
            lambda _task_id, event, _data: order.append(f"publish_{event}"),
        )

        chapter_routes._finalize_chapter_task(
            service,
            "ws-demo",
            "ch-0003",
            "task-order",
            result={"ok": True},
        )

        assert order == ["lease_complete", "task_done", "publish_done"]


# =============================================================================
class TestReviewSummaryExcellenceMetrics:
    """``ReviewSummaryEntry`` 暴露 wave 5 卓越性度量(v1.1 wave 7 / B)。

    score_imagery / score_interiority / score_formulaic / score_immersion 从内部
    ``ReviewResult`` 透传到 API 输出,供前端"质感仪表盘"展示。**防回退**:wave 5
    时这 4 字段刻意"零前端"(不进 API schema);wave 7 B 把它们序列化出去供展示。
    本测钉住"4 字段透传 + None 兜底 + JSON 可见",防后续改动误删 schema 字段或
    转换点映射。**不变量**(它们仍永不进 score_overall、不影响 verdict)由
    reviewer 测覆盖,本测只验序列化透传。
    """

    def _make_review_result(self, **overrides):  # type: ignore[no-untyped-def]
        from datetime import datetime, timezone

        from src.core.models.review import ReviewResult

        base = dict(
            review_id="rev-excellence",
            chapter_id="ch-0001",
            version_reviewed=1,
            reviewed_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            reviewer_model="mock-default",
            verdict="pass",
            score_overall=8.0,
            score_plot_progression=8.0,
            score_tension_match=8.0,
            score_prose_quality=8.0,
            score_consistency=8.0,
            score_hook_management=8.0,
            score_voice_consistency=8.0,
            hard_constraints_passed=True,
            failed_constraints=[],
            issues=[],
            suggestions=[],
            revise_targets=[],
        )
        base.update(overrides)
        return ReviewResult(**base)

    def test_excellence_metrics_passed_through(self) -> None:
        from src.api.routes.chapters import _review_summary_entry_from_result

        r = self._make_review_result(
            score_imagery=8.5,
            score_interiority=7.0,
            score_formulaic=2.5,
            score_immersion=9.0,
        )
        entry = _review_summary_entry_from_result(r)
        assert entry.score_imagery == pytest.approx(8.5)
        assert entry.score_interiority == pytest.approx(7.0)
        assert entry.score_formulaic == pytest.approx(2.5)
        assert entry.score_immersion == pytest.approx(9.0)

    def test_excellence_metrics_none_when_reviewer_omits(self) -> None:
        """LLM 漏产 / 主审退化 → 4 字段为 None(向后兼容,旧 review 无此字段)。"""
        from src.api.routes.chapters import _review_summary_entry_from_result

        entry = _review_summary_entry_from_result(self._make_review_result())
        assert entry.score_imagery is None
        assert entry.score_interiority is None
        assert entry.score_formulaic is None
        assert entry.score_immersion is None

    def test_excellence_metrics_serialize_to_json(self) -> None:
        """端到端:model_dump 后 4 字段在 JSON 里(前端 fetch 拿得到)。"""
        from src.api.routes.chapters import _review_summary_entry_from_result

        r = self._make_review_result(score_imagery=8.5, score_formulaic=1.0)
        dumped = _review_summary_entry_from_result(r).model_dump()
        assert dumped["score_imagery"] == pytest.approx(8.5)
        assert dumped["score_formulaic"] == pytest.approx(1.0)
        assert dumped["score_interiority"] is None
        assert dumped["score_immersion"] is None

    def test_constraint_violations_serialize_to_json(self) -> None:
        """R2 结构化违规经现有 review summary API 原样可复查。"""
        from src.api.routes.chapters import _review_summary_entry_from_result
        from src.core.models.review import ConstraintViolation

        violation = ConstraintViolation(
            constraint_id="e" * 64,
            domain="world.unique_rules",
            source_path="data/outline/world.json",
            source_hash="f" * 64,
            rule_quote="记忆抹除不可逆。",
            evidence_quote="被抹除的记忆全部恢复。",
            severity="hard",
            ambiguity=None,
        )
        dumped = _review_summary_entry_from_result(
            self._make_review_result(
                verdict="rewrite",
                hard_constraints_passed=False,
                constraint_violations=[violation],
            )
        ).model_dump()
        assert dumped["constraint_violations"] == [violation.model_dump(mode="python")]

    def test_constraint_results_and_regression_ids_serialize_to_json(self) -> None:
        """R6 review summary 使用同一稳定 ID，不从展示文案反解析。"""
        from src.api.routes.chapters import _review_summary_entry_from_result
        from src.core.models.review import ConstraintResult, stable_constraint_id

        source_path = "chapter_cards/ch-0001.forbidden"
        rule_text = "禁止提前揭示最终真相"
        constraint_id = stable_constraint_id(
            source_path=source_path,
            domain="forbidden",
            rule_text=rule_text,
        )
        result = ConstraintResult(
            constraint_id=constraint_id,
            domain="forbidden",
            source_path=source_path,
            rule_text=rule_text,
            status="fail",
            details="展示文案可变化",
        )
        dumped = _review_summary_entry_from_result(
            self._make_review_result(
                verdict="rewrite",
                hard_constraints_passed=False,
                constraint_results=[result],
                hard_constraint_regressions=[constraint_id],
            )
        ).model_dump()
        assert dumped["constraint_results"] == [result.model_dump(mode="python")]
        assert dumped["hard_constraint_regressions"] == [constraint_id]


# =============================================================================
# 1. :write
# =============================================================================


class TestWrite:
    def test_write_main_path_returns_done_task_info(
        self,
        client: TestClient,
        mock_client: MagicMock,
        prepared: str,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_resp(content="第一稿"),
            _reviewer_resp(score=8.0),  # pass
        ]
        resp = client.post("/api/v1/chapters/ch-0002:write", headers=_hdr())
        # v1.0.x Commit B(2026-05-21):202 + 后台跑 + registry done
        _, result = _assert_async_task_done(resp, expected_type="chapter_write")
        # result 段是 WriteResultSummary 摘要
        assert result["final_state"] == "approved"
        assert result["final_version_id"] == 1
        assert result["final_path"].endswith("ch-0002/final.md")
        assert result["version_count"] == 1
        assert result["revise_count"] == 0
        assert result["rewrite_count"] == 0
        assert result["escalation_reason"] is None
        # review_summary 每条带 revise_targets(决策 6 补充)
        rs = result["review_summary"]
        assert len(rs) == 1
        assert rs[0]["verdict"] == "pass"
        assert rs[0]["score_overall"] == pytest.approx(8.0)
        assert isinstance(rs[0]["revise_targets"], list)
        assert "draft_pool" not in result

    def test_write_with_revise_then_pass_serializes_revise_targets(
        self,
        client: TestClient,
        mock_client: MagicMock,
        prepared: str,
    ) -> None:
        """覆盖 verdict=revise 后非空 revise_targets 的序列化(P1-B-23 验收暴露)。

        ``ReviewIssueOut`` 是 ``ReviewIssue`` 的空子类,Pydantic v2 不 coerce
        父类实例 → 子类字段。``_build_write_summary`` 必须显式 ``model_validate``
        把 ``ReviewIssue`` 转 ``ReviewIssueOut``,否则 verdict=revise 路径
        响应序列化时抛 ValidationError → HTTP 500。
        """
        mock_client.complete.side_effect = contract_response_sequence(
            _writer_resp(content="第一稿"),
            _reviewer_resp(
                score=7.0,
                paragraph_anchor="第一稿",
            ),  # 6.5 ≤ 7.0 < 7.5 → revise
            lambda messages: single_target_localized_response(
                messages,
                replacement="修订后",
                input_tokens=2500,
                output_tokens=2200,
            ),
            _reviewer_resp(score=8.0),  # 8.0 ≥ 7.5 → pass
        )
        resp = client.post("/api/v1/chapters/ch-0002:write", headers=_hdr())
        # v1.0.x Commit B:202 + 后台跑 + registry done
        _, result = _assert_async_task_done(resp, expected_type="chapter_write")
        assert result["final_state"] == "approved"
        assert result["version_count"] == 2  # draft + revise
        assert result["revise_count"] == 1
        assert result["rewrite_count"] == 0
        rs = result["review_summary"]
        assert len(rs) == 2
        # 第一次审稿:revise + 非空 revise_targets(本 bug 暴露点)
        assert rs[0]["verdict"] == "revise"
        assert len(rs[0]["revise_targets"]) == 1
        assert rs[0]["revise_targets"][0]["issue_type"] == "pacing"
        assert rs[0]["revise_targets"][0]["paragraph_anchor"]
        # 第二次审稿:pass + 空 revise_targets
        assert rs[1]["verdict"] == "pass"
        assert rs[1]["revise_targets"] == []

    def test_write_missing_card_returns_card_not_found(
        self, client: TestClient, workspace_service: WorkspaceService
    ) -> None:
        # 创建工作区,但**不**落章节卡 → 路由前置守卫抛 404
        # v1.0.x Commit B:_ensure_card_exists 在路由层 sync 抛 ApiError,
        # 仍走 HTTP 404 同步路径(BackgroundTasks 未启动)。
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        resp = client.post("/api/v1/chapters/ch-missing:write", headers=_hdr())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHAPTER_CARD_NOT_FOUND"

    def test_write_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/v1/chapters/ch-0002:write")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 2. :rewrite
# =============================================================================


class TestRewrite:
    def test_background_task_forwards_api_task_id_into_attempt_scope(self) -> None:
        from src.api.routes.chapters import _run_chapter_rewrite_task

        service = MagicMock(spec=ChapterService)
        service.rewrite.side_effect = RuntimeError("capture only")
        task_id = "task_r8_ui_rewrite"
        task_registry.record_pending(
            task_id,
            type="chapter_rewrite",
            workspace_id="ws-demo",
        )

        _run_chapter_rewrite_task(
            service,
            "ws-demo",
            "ch-0003",
            task_id,
            prior_rewrite_count=0,
            profile_override=None,
        )

        assert service.rewrite.call_args.kwargs["attempt_task_id"] == task_id

    def test_rewrite_main_path(
        self,
        client: TestClient,
        mock_client: MagicMock,
        prepared: str,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_resp(content="重写稿"),
            _reviewer_resp(score=8.0),
        ]
        resp = client.post(
            "/api/v1/chapters/ch-0002:rewrite",
            headers=_hdr(),
            json={"prior_rewrite_count": 0},
        )
        _, result = _assert_async_task_done(resp, expected_type="chapter_rewrite")
        assert result["final_state"] == "approved"
        # R5:显式 rewrite 本身消费一次 content rewrite，返回值不能继续沿用
        # 旧的调用前计数。
        assert result["rewrite_count"] == 1

    def test_rewrite_default_prior_rewrite_count_is_zero(
        self,
        client: TestClient,
        mock_client: MagicMock,
        prepared: str,
    ) -> None:
        """空 body 应被视为 prior_rewrite_count=0(Pydantic 默认值)。"""
        mock_client.complete.side_effect = [
            _writer_resp(content="重写稿"),
            _reviewer_resp(score=8.0),
        ]
        resp = client.post("/api/v1/chapters/ch-0002:rewrite", headers=_hdr(), json={})
        _assert_async_task_done(resp, expected_type="chapter_rewrite")

    def test_rewrite_prior_count_exhausts_budget_escalates(
        self,
        client: TestClient,
        mock_client: MagicMock,
        workspace_service: WorkspaceService,
        chapter_service: ChapterService,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        chapter_service.save_chapter_card("ws-demo", _card(max_rewrites=1))
        mock_client.complete.side_effect = [
            _writer_resp(content="重写稿"),
            _reviewer_resp(score=5.0),
        ]
        resp = client.post(
            "/api/v1/chapters/ch-0002:rewrite",
            headers=_hdr(),
            json={"prior_rewrite_count": 1},
        )
        _, result = _assert_async_task_done(resp, expected_type="chapter_rewrite")
        assert result["final_state"] == "human_intervention"
        assert result["escalation_reason"] == "rewrite_budget_exhausted"


# =============================================================================
# 2.5 :re-review(v2 W3.5 UI 缺口3)
# =============================================================================


class TestReReview:
    """人工改稿后重审端点路由层。

    **happy / fail 端到端(状态从 DB 读)在 wired 服务层测**
    (``tests/services/test_chapter_service_wiring.py::TestReReviewWiring``):本
    路由 fixture 无 session_factory,章节状态不落库 → ``status`` 恒非
    human_intervention,只能验"路由 → service guard → 错误翻译 → task failed"
    这条 plumbing。
    """

    def test_re_review_wrong_state_fails(
        self, client: TestClient, prepared: str
    ) -> None:
        """章节非 human_intervention → 任务失败 CHAPTER_RE_REVIEW_INVALID_STATE
        (路由 + service guard + 错误翻译 + 异步 failed publish 全链路)。"""
        resp = client.post(
            "/api/v1/chapters/ch-0002:re-review", headers=_hdr(), json={}
        )
        _assert_async_task_failed(
            resp,
            expected_type="chapter_re_review",
            expected_code="CHAPTER_RE_REVIEW_INVALID_STATE",
        )


# =============================================================================
# 3. :revise
# =============================================================================


class TestRevise:
    def test_revise_main_path_returns_chapter_version_meta(
        self,
        client: TestClient,
        mock_client: MagicMock,
        chapter_service: ChapterService,
        prepared: str,
    ) -> None:
        # 先 write 把 v1 落出来
        mock_client.complete.side_effect = [
            _writer_resp(content="原稿"),
            _reviewer_resp(score=8.0),
        ]
        chapter_service.write(prepared, "ch-0002")
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = contract_response_sequence(
            lambda messages: single_target_localized_response(
                messages,
                replacement="修订稿",
                input_tokens=2500,
                output_tokens=2200,
            )
        )
        resp = client.post(
            "/api/v1/chapters/ch-0002:revise",
            headers=_hdr(),
            json={
                "issues": [
                    {
                        "paragraph_anchor": "原稿",
                        "issue_type": "pacing",
                        "description": "节奏过缓",
                        "suggestion": "压缩",
                    }
                ]
            },
        )
        _, meta = _assert_async_task_done(resp, expected_type="chapter_revise")
        assert meta["version_id"] == 2
        assert meta["creation_type"] == "revise"
        assert meta["word_count"] == len("修订稿")
        assert "content" not in meta  # 决策 6:不含正文

    def test_revise_no_prior_version_returns_invalid_state(
        self, client: TestClient, prepared: str
    ) -> None:
        # 没跑过任何 write,revise 时 ChapterServiceError("尚无任何版本") →
        # CHAPTER_INVALID_STATE (409)
        # v1.0.x Commit B:Service 异常在 BackgroundTasks 内捕获 → task_registry
        # update_to_failed(error.code="CHAPTER_INVALID_STATE"),HTTP 仍 202。
        resp = client.post(
            "/api/v1/chapters/ch-0002:revise",
            headers=_hdr(),
            json={"issues": []},
        )
        _assert_async_task_failed(
            resp,
            expected_type="chapter_revise",
            expected_code="CHAPTER_INVALID_STATE",
        )


# =============================================================================
# 4. :switch-version
# =============================================================================


class TestSwitchVersion:
    def test_switch_version_main_path(
        self,
        client: TestClient,
        mock_client: MagicMock,
        chapter_service: ChapterService,
        prepared: str,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_resp(content="v1 内容"),
            _reviewer_resp(score=8.0),
        ]
        chapter_service.write(prepared, "ch-0002")
        resp = client.post(
            "/api/v1/chapters/ch-0002:switch-version",
            headers=_hdr(),
            json={"version_id": 1},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["chapter_id"] == "ch-0002"
        assert data["version_id"] == 1
        assert data["final_path"].endswith("ch-0002/final.md")

    def test_switch_version_missing_returns_version_conflict(
        self, client: TestClient, prepared: str
    ) -> None:
        # 没写过任何版本 → mark_final 抛 ChapterVersioningError → 409
        resp = client.post(
            "/api/v1/chapters/ch-0002:switch-version",
            headers=_hdr(),
            json={"version_id": 99},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CHAPTER_VERSION_CONFLICT"


# =============================================================================
# 5. GET /status
# =============================================================================


class TestStatus:
    """7 档 status 路由层断言(P2-B-17 起;fixture 默认无 session_factory,
    走 fs fallback,无版本+无 final → ``drafting``)。DB-priority 路径在
    ``TestStatusDbBacked`` 用注入的 session_factory 覆盖。"""

    def test_status_no_versions_maps_to_drafting(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.get("/api/v1/chapters/ch-0002/status", headers=_hdr())
        assert resp.status_code == 200
        data = resp.json()["data"]
        # 7 档 fs fallback:无 final + 无版本 → drafting(P1 ``not_started`` 折算)
        assert data["status"] == "drafting"
        assert data["latest_version_id"] is None
        assert data["has_final"] is False
        assert data["version_count"] == 0

    def test_status_approved_after_write(
        self,
        client: TestClient,
        mock_client: MagicMock,
        chapter_service: ChapterService,
        prepared: str,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_resp(content="v1"),
            _reviewer_resp(score=8.0),
        ]
        chapter_service.write(prepared, "ch-0002")
        resp = client.get("/api/v1/chapters/ch-0002/status", headers=_hdr())
        data = resp.json()["data"]
        assert data["status"] == "approved"
        assert data["has_final"] is True
        assert data["version_count"] == 1


class TestStatusDbBacked:
    """``session_factory`` 注入后 ``GET /chapters/{id}/status`` 返回 DB
    持久化的 7 档 state(P2-B-17),不再走 fs fallback。

    验证关键场景:无 final.md 但 chapters.status='reviewing' →
    路由层返 'reviewing'(若读 fs 会返 'drafting',不一致即 DB 优先生效)。
    """

    def test_get_status_returns_db_state_over_fs(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        from datetime import datetime, timezone

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.api.deps import get_chapter_service as _dep_get_chapter_service
        from src.api.main import create_app
        from src.core.db import Base, ChapterORM, WorkspaceORM
        from src.core.workspace.workspace_paths import WorkspacePaths

        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )

        paths = WorkspacePaths(
            "ws-demo", workspaces_root=workspace_service.workspaces_root
        )
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
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
        # 章节卡也得有(GET /status 不读 card,但 list_chapters 走 cards 目录)。
        service.save_chapter_card("ws-demo", _card())

        app = create_app()
        app.dependency_overrides[_dep_get_chapter_service] = lambda: service
        with TestClient(app) as test_client:
            resp = test_client.get("/api/v1/chapters/ch-0002/status", headers=_hdr())
            assert resp.status_code == 200
            data = resp.json()["data"]
            # DB-priority:返 'reviewing' 而非 fs fallback 的 'drafting'
            assert data["status"] == "reviewing"
            assert data["has_final"] is False
            assert data["version_count"] == 0


# =============================================================================
# 6. /card CRUD
# =============================================================================


class TestCard:
    def test_get_card_main_path(self, client: TestClient, prepared: str) -> None:
        resp = client.get("/api/v1/chapters/ch-0002/card", headers=_hdr())
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["chapter_id"] == "ch-0002"
        assert data["workspace_id"] == "ws-demo"

    def test_get_card_missing_returns_card_not_found(
        self, client: TestClient, workspace_service: WorkspaceService
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        resp = client.get("/api/v1/chapters/ch-ghost/card", headers=_hdr())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHAPTER_CARD_NOT_FOUND"

    def test_put_card_main_path(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        chapter_service: ChapterService,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        card_payload = json.loads(_card(chapter_id="ch-0010").model_dump_json())
        resp = client.put(
            "/api/v1/chapters/ch-0010/card",
            headers=_hdr(),
            json=card_payload,
        )
        assert resp.status_code == 200
        loaded = chapter_service.read_chapter_card("ws-demo", "ch-0010")
        assert loaded.chapter_id == "ch-0010"

    def test_put_card_id_mismatch_returns_422(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        card_payload = json.loads(_card(chapter_id="ch-aaa").model_dump_json())
        resp = client.put(
            "/api/v1/chapters/ch-bbb/card",
            headers=_hdr(),
            json=card_payload,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CHAPTER_ID_MISMATCH"

    @pytest.mark.parametrize("retired", ["single", "parallel_2", "parallel_3"])
    def test_put_card_rejects_retired_draft_strategy(
        self,
        retired: str,
        client: TestClient,
        workspace_service: WorkspaceService,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        card_payload = json.loads(_card(chapter_id="ch-0011").model_dump_json())
        card_payload["draft_strategy"] = retired
        resp = client.put(
            "/api/v1/chapters/ch-0011/card",
            headers=_hdr(),
            json=card_payload,
        )
        assert resp.status_code == 422

    def test_put_card_invalid_workspace_returns_workspace_conflict(
        self, client: TestClient
    ) -> None:
        # 工作区不存在 → ChapterService.save 调 _validate_workspace 翻译 →
        # WorkspaceServiceError → 409
        card_payload = json.loads(_card(chapter_id="ch-0002").model_dump_json())
        resp = client.put(
            "/api/v1/chapters/ch-0002/card",
            headers={"X-Workspace-Id": "no_such_ws"},
            json=card_payload,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "WORKSPACE_CONFLICT"


# =============================================================================
# B-01 hot-fix:GET /chapters/{id}/review-history
# =============================================================================


class TestReviewHistory:
    """B-01 hot-fix:从 review_results 表查持久化历史(替代 task_registry LRU)。"""

    def test_empty_returns_empty_list(
        self, client: TestClient, workspace_service: WorkspaceService
    ) -> None:
        """无 review 行 → 返空列表 200(不抛 404)。"""
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        resp = client.get("/api/v1/chapters/ch-0001/review-history", headers=_hdr())
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_returns_history_with_revise_targets(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """seed 2 reviews + 1 revise_target → 返按 reviewed_at 升序结果。

        沿用 TestStatusDbBacked 同模式:注入 session_factory + 自建 app +
        覆盖 dependency,因 chapter_service fixture 默认无 session_factory。
        """
        from datetime import datetime, timezone

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.api.deps import get_chapter_service as _dep_get_chapter_service
        from src.api.main import create_app
        from src.core.db import (
            Base,
            ChapterORM,
            ReviewIssueORM,
            ReviewResultORM,
        )
        from src.core.workspace.workspace_paths import WorkspacePaths
        from src.services.asset_import import ensure_workspace_orm

        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        paths = WorkspacePaths(
            "ws-demo", workspaces_root=workspace_service.workspaces_root
        )
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        now = datetime.now(timezone.utc)
        with _Session(engine) as session:
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
                    review_id="rev-a",
                    chapter_id="ch-0001",
                    version_reviewed=1,
                    reviewed_at=now.replace(microsecond=100000),
                    reviewer_model="claude-sonnet-4-6",
                    score_plot_progression=7.0,
                    score_tension_match=6.5,
                    score_prose_quality=7.0,
                    score_consistency=8.0,
                    score_hook_management=7.0,
                    score_voice_consistency=7.2,
                    score_overall=7.12,
                    hard_constraints_passed=True,
                    failed_constraints=[],
                    suggestions=[],
                    verdict="revise",
                )
            )
            session.add(
                ReviewResultORM(
                    review_id="rev-b",
                    chapter_id="ch-0001",
                    version_reviewed=2,
                    reviewed_at=now.replace(microsecond=200000),
                    reviewer_model="claude-sonnet-4-6",
                    score_plot_progression=8.0,
                    score_tension_match=7.0,
                    score_prose_quality=8.0,
                    score_consistency=8.5,
                    score_hook_management=8.0,
                    score_voice_consistency=8.0,
                    score_overall=7.92,
                    hard_constraints_passed=True,
                    failed_constraints=[],
                    suggestions=[],
                    verdict="pass",
                )
            )
            session.add(
                ReviewIssueORM(
                    review_id="rev-a",
                    role="revise_target",
                    position=0,
                    paragraph_anchor="开头",
                    issue_type="pacing",
                    description="慢",
                    suggestion="快",
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

        app = create_app()
        app.dependency_overrides[_dep_get_chapter_service] = lambda: service
        with TestClient(app) as test_client:
            resp = test_client.get(
                "/api/v1/chapters/ch-0001/review-history", headers=_hdr()
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        assert data[0]["verdict"] == "revise"
        assert data[0]["version_reviewed"] == 1
        assert len(data[0]["revise_targets"]) == 1
        assert data[0]["revise_targets"][0]["issue_type"] == "pacing"
        assert data[1]["verdict"] == "pass"
        assert data[1]["revise_targets"] == []

    def test_review_history_includes_excellence_metrics(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
    ) -> None:
        """v1.1 wave 7 / B 持久化:review-history 端点透传 4 卓越性度量(端到端)。

        seed 一行带 4 度量 + 一行不带(老数据,NULL)→ 端点:有值的返值、
        NULL 的返 None(get_review_summary 读映射 + ReviewSummaryEntry 序列化)。
        """
        from datetime import datetime, timezone

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _Session

        from src.api.deps import get_chapter_service as _dep_get_chapter_service
        from src.api.main import create_app
        from src.core.db import Base, ChapterORM, ReviewResultORM
        from src.core.workspace.workspace_paths import WorkspacePaths
        from src.services.asset_import import ensure_workspace_orm

        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        paths = WorkspacePaths(
            "ws-demo", workspaces_root=workspace_service.workspaces_root
        )
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        now = datetime.now(timezone.utc)
        base_scores = dict(
            score_plot_progression=8.0,
            score_tension_match=8.0,
            score_prose_quality=8.0,
            score_consistency=8.0,
            score_hook_management=8.0,
            score_voice_consistency=8.0,
            score_overall=8.0,
            hard_constraints_passed=True,
            failed_constraints=[],
            suggestions=[],
            verdict="pass",
        )
        with _Session(engine) as session:
            ensure_workspace_orm(session, workspace_service.info("ws-demo"))
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id="ws-demo",
                    current_version=2,
                    status="approved",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
            session.add(
                ReviewResultORM(  # v1:带 4 度量
                    review_id="rev-metrics",
                    chapter_id="ch-0001",
                    version_reviewed=1,
                    reviewed_at=now.replace(microsecond=100000),
                    reviewer_model="m",
                    score_imagery=8.5,
                    score_interiority=7.0,
                    score_formulaic=2.5,
                    score_immersion=9.0,
                    **base_scores,
                )
            )
            session.add(
                ReviewResultORM(  # v2:不带(老数据 → NULL)
                    review_id="rev-null",
                    chapter_id="ch-0001",
                    version_reviewed=2,
                    reviewed_at=now.replace(microsecond=200000),
                    reviewer_model="m",
                    **base_scores,
                )
            )
            session.commit()

        def _factory(_ws: str) -> _Session:
            return _Session(engine)

        service = ChapterService(
            workspace_service=workspace_service,
            client=mock_client,
            model="m",
            session_factory=_factory,
        )
        app = create_app()
        app.dependency_overrides[_dep_get_chapter_service] = lambda: service
        with TestClient(app) as test_client:
            resp = test_client.get(
                "/api/v1/chapters/ch-0001/review-history", headers=_hdr()
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        # v1 带度量 → 透传值
        assert data[0]["score_imagery"] == pytest.approx(8.5)
        assert data[0]["score_interiority"] == pytest.approx(7.0)
        assert data[0]["score_formulaic"] == pytest.approx(2.5)
        assert data[0]["score_immersion"] == pytest.approx(9.0)
        # v2 老数据 → None(向后兼容)
        assert data[1]["score_imagery"] is None
        assert data[1]["score_formulaic"] is None

    def test_missing_workspace_header_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/chapters/ch-0001/review-history")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 7. /versions
# =============================================================================


class TestVersions:
    def test_list_versions_empty_when_no_writes(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.get("/api/v1/chapters/ch-0002/versions", headers=_hdr())
        assert resp.status_code == 200
        assert resp.json()["data"] == {"chapter_id": "ch-0002", "versions": []}

    def test_list_versions_returns_all_after_write(
        self,
        client: TestClient,
        mock_client: MagicMock,
        chapter_service: ChapterService,
        prepared: str,
    ) -> None:
        # write 跑一轮(revise 一次再 pass)→ v1 + v2
        mock_client.complete.side_effect = contract_response_sequence(
            _writer_resp(content="v1"),
            _reviewer_resp(score=7.0, paragraph_anchor="v1"),  # revise
            lambda messages: single_target_localized_response(
                messages,
                replacement="v2",
                input_tokens=2500,
                output_tokens=2200,
            ),
            _reviewer_resp(score=8.0),  # pass
        )
        chapter_service.write(prepared, "ch-0002")
        resp = client.get("/api/v1/chapters/ch-0002/versions", headers=_hdr())
        ids = [v["version_id"] for v in resp.json()["data"]["versions"]]
        assert ids == [1, 2]

    def test_get_version_content_main_path(
        self,
        client: TestClient,
        mock_client: MagicMock,
        chapter_service: ChapterService,
        prepared: str,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_resp(content="第一稿正文"),
            _reviewer_resp(score=8.0),
        ]
        chapter_service.write(prepared, "ch-0002")
        resp = client.get("/api/v1/chapters/ch-0002/versions/1", headers=_hdr())
        data = resp.json()["data"]
        assert data["version_id"] == 1
        assert data["content"] == "第一稿正文"
        assert data["word_count"] == len("第一稿正文")

    def test_get_version_missing_returns_409(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.get("/api/v1/chapters/ch-0002/versions/99", headers=_hdr())
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CHAPTER_VERSION_CONFLICT"


# =============================================================================
# 8. /tasks/{id}
# =============================================================================


class TestTasks:
    def test_get_task_after_write_hits_lru(
        self,
        client: TestClient,
        mock_client: MagicMock,
        prepared: str,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_resp(content="v1"),
            _reviewer_resp(score=8.0),
        ]
        write_resp = client.post("/api/v1/chapters/ch-0002:write", headers=_hdr())
        task_id = write_resp.json()["data"]["task_id"]
        get_resp = client.get(f"/api/v1/tasks/{task_id}")
        assert get_resp.status_code == 200
        info = get_resp.json()["data"]
        assert info["task_id"] == task_id
        assert info["status"] == "done"
        assert info["type"] == "chapter_write"

    def test_get_task_missing_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/tasks/task_nonexistent_xyz")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TASK_NOT_FOUND"


# =============================================================================
# 9. GET /chapters 列表(P1-B-25)
# =============================================================================


class TestListChapters:
    def test_list_empty_workspace_returns_empty_list(
        self, client: TestClient, workspace_service: WorkspaceService
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        resp = client.get("/api/v1/chapters", headers=_hdr())
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert "error" not in body

    def test_list_returns_multiple_cards_sorted_by_chapter_number(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        chapter_service: ChapterService,
    ) -> None:
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        # 故意乱序落 3 张卡:ch-0003 / ch-0001 / ch-0002
        chapter_service.save_chapter_card(
            "ws-demo",
            _card(chapter_id="ch-0003", chapter_number=3, title="第三章"),
        )
        chapter_service.save_chapter_card(
            "ws-demo",
            _card(chapter_id="ch-0001", chapter_number=1, title="第一章"),
        )
        chapter_service.save_chapter_card(
            "ws-demo",
            _card(chapter_id="ch-0002", chapter_number=2, title="第二章"),
        )
        resp = client.get("/api/v1/chapters", headers=_hdr())
        assert resp.status_code == 200
        items = resp.json()["data"]
        assert len(items) == 3
        # 按 chapter_number 升序
        assert [it["chapter_id"] for it in items] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        assert [it["chapter_number"] for it in items] == [1, 2, 3]
        assert [it["title"] for it in items] == [
            "第一章",
            "第二章",
            "第三章",
        ]
        # 都未跑 write,fs fallback 推断 → drafting(P2-B-17 起 7 档,
        # 旧 P1 ``not_started`` 在 7 档中折算到状态机起点 ``drafting``)
        for it in items:
            assert it["status"] == "drafting"
            assert it["latest_version_id"] is None
            assert it["has_final"] is False
            assert it["version_count"] == 0
            assert it["volume_id"] == "v01"
            # last_modified_at 为 ISO 8601 UTC,以 'Z' 结尾
            assert it["last_modified_at"].endswith("Z")

    def test_list_status_inference_after_write(
        self,
        client: TestClient,
        mock_client: MagicMock,
        workspace_service: WorkspaceService,
        chapter_service: ChapterService,
    ) -> None:
        """write 跑完一章 → status=approved;未跑章 → drafting(P2-B-17 起 7 档)。"""
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        chapter_service.save_chapter_card(
            "ws-demo", _card(chapter_id="ch-0001", chapter_number=1)
        )
        chapter_service.save_chapter_card(
            "ws-demo", _card(chapter_id="ch-0002", chapter_number=2)
        )
        mock_client.complete.side_effect = [
            _writer_resp(content="v1"),
            _reviewer_resp(score=8.0),
        ]
        chapter_service.write("ws-demo", "ch-0001")

        resp = client.get("/api/v1/chapters", headers=_hdr())
        assert resp.status_code == 200
        items = resp.json()["data"]
        by_id = {it["chapter_id"]: it for it in items}
        assert by_id["ch-0001"]["status"] == "approved"
        assert by_id["ch-0001"]["has_final"] is True
        assert by_id["ch-0001"]["version_count"] == 1
        assert by_id["ch-0001"]["latest_version_id"] == 1
        # 未跑 write → fs fallback 推断到 drafting(P1 ``not_started`` 折算)
        assert by_id["ch-0002"]["status"] == "drafting"
        assert by_id["ch-0002"]["latest_version_id"] is None

    def test_list_skips_invalid_card_files(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        chapter_service: ChapterService,
    ) -> None:
        """损坏的章节卡 JSON 不阻塞列表,合法卡正常返。"""
        workspace_service.create(
            "ws-demo", display_name="x", genre="scifi", premise="y"
        )
        chapter_service.save_chapter_card(
            "ws-demo", _card(chapter_id="ch-0001", chapter_number=1)
        )
        # 故意往 cards_dir 塞一张 JSON 损坏的卡
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(
            "ws-demo", workspaces_root=workspace_service.workspaces_root
        )
        bad = paths.cards_dir / "ch-bad.json"
        bad.write_text("{not json", encoding="utf-8")
        # 再塞一张 schema 不合法的卡(JSON 合法但缺字段)
        invalid = paths.cards_dir / "ch-incomplete.json"
        invalid.write_text('{"chapter_id": "ch-incomplete"}', encoding="utf-8")

        resp = client.get("/api/v1/chapters", headers=_hdr())
        assert resp.status_code == 200
        items = resp.json()["data"]
        # 只返合法的 ch-0001,坏卡跳过
        assert [it["chapter_id"] for it in items] == ["ch-0001"]

    def test_list_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/chapters")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_list_invalid_workspace_returns_workspace_conflict(
        self, client: TestClient
    ) -> None:
        # 工作区不存在 → service _validate_workspace 抛 WorkspaceServiceError → 409
        resp = client.get("/api/v1/chapters", headers={"X-Workspace-Id": "no_such_ws"})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "WORKSPACE_CONFLICT"


# =============================================================================
# 10. OpenAPI
# =============================================================================


class TestOpenAPI:
    def test_openapi_includes_all_chapter_and_task_routes(
        self, client: TestClient
    ) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        expected = [
            "/api/v1/chapters",
            "/api/v1/chapters/{chapter_id}:write",
            "/api/v1/chapters/{chapter_id}:rewrite",
            "/api/v1/chapters/{chapter_id}:revise",
            "/api/v1/chapters/{chapter_id}:switch-version",
            "/api/v1/chapters/{chapter_id}/status",
            "/api/v1/chapters/{chapter_id}/card",
            "/api/v1/chapters/{chapter_id}/versions",
            "/api/v1/chapters/{chapter_id}/versions/{version_id}",
            "/api/v1/tasks/{task_id}",
        ]
        for p in expected:
            assert p in paths, f"OpenAPI 缺路由 {p}"


# ---------------------------------------------------------------------------
# E6:POST /chapters:refresh-continuity-all(v1.1 w3-s7)
# ---------------------------------------------------------------------------


class TestRefreshContinuityAll:
    """批量 continuity backfill 端点:envelope 包装 + 逐章 outcome + 缺 header 422。"""

    def test_refresh_continuity_all_envelope(
        self, client: TestClient, prepared: str
    ) -> None:
        """prepared 只有 ch-0002(无 ch-0001)→ 无上一章 → unchanged。"""
        resp = client.post("/api/v1/chapters:refresh-continuity-all", headers=_hdr())
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["workspace_id"] == "ws-demo"
        assert data["total_scanned"] == 1
        assert data["refreshed_count"] == 0
        assert data["items"] == [
            {
                "chapter_id": "ch-0002",
                "chapter_number": 2,
                "outcome": "unchanged",
            }
        ]

    def test_refresh_continuity_all_missing_header_422(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.post("/api/v1/chapters:refresh-continuity-all")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# H7:GET /chapters/{id}/style-drift(v1.1 w3-s7)
# ---------------------------------------------------------------------------


class TestStyleDriftRoute:
    """风格指纹时序端点:envelope 包装 + 无 session 空 points + 缺 header 422。

    (本测试 fixture 无 session_factory → points 空;populated 派生逻辑由
    service 层 TestH7StyleFingerprintSeries 覆盖。)
    """

    def test_style_drift_envelope_empty(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.get("/api/v1/chapters/ch-0002/style-drift", headers=_hdr())
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data == {"chapter_id": "ch-0002", "points": []}

    def test_style_drift_missing_header_422(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.get("/api/v1/chapters/ch-0002/style-drift")
        assert resp.status_code == 422


class TestIgnoreFeasibility:
    """v2 Fix 2:``:ignore-feasibility`` 端点(人工确认 Fix D 误判 → 跳过写前预检)。

    守卫核心:仅 ``human_intervention`` 孤儿章可调,其余 status 返 409
    ``CHAPTER_INVALID_STATE``（防误用于健康 / 已完成章）。
    """

    def test_non_orphan_returns_409(self, client: TestClient, prepared: str) -> None:
        # 全新章 ch-0002 status 派生 drafting(非 human_intervention)→ 409。
        resp = client.post(
            "/api/v1/chapters/ch-0002:ignore-feasibility", headers=_hdr()
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CHAPTER_INVALID_STATE"

    def test_missing_card_returns_404(self, client: TestClient, prepared: str) -> None:
        resp = client.post(
            "/api/v1/chapters/ch-missing:ignore-feasibility", headers=_hdr()
        )
        assert resp.status_code == 404

    def test_missing_header_422(self, client: TestClient, prepared: str) -> None:
        resp = client.post("/api/v1/chapters/ch-0002:ignore-feasibility")
        assert resp.status_code == 422
