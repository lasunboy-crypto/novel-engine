"""WebSocket 基础连接 + 订阅 + 推送 + 心跳测试(P1-B-20)。

覆盖
====

1. 连接 + 订阅 + 手动 ``manager.publish`` → 收到 ``state_transition`` 事件
2. 取消订阅后不再收
3. 多频道并行订阅
4. 未知 ``type`` 静默忽略不关连接
5. ``WebSocketDisconnect`` 时清理连接
6. ``ping/pong`` 心跳:回 pong 保持连接;不回 pong 超阈值 → 关连接
7. 路由触发(``POST /chapters/{id}:write``)端到端 → 收到 state_transition 事件
8. 频道命名 helper 字面格式
9. Manager 单元(无订阅者 publish noop / 未知 conn 静默 / 消息格式合规)

关键测试细节
============

- TestClient 的 WebSocket 是异步的:client 线程的 ``send_json`` 入队后,
  服务端在自己的事件循环里 ``await receive_json``,二者之间**没有同步往返**。
  ``subscribe`` 之后不能立即 publish——服务端可能尚未 dispatch。
  用 ``_wait_for_subscription`` 轮询 manager 内部状态规避竞态。
- ``HEARTBEAT_INTERVAL_SECONDS`` / ``HEARTBEAT_MAX_MISSED`` 心跳测试用很短的
  间隔(0.05s / 1)以保证快速完成。
- ``manager.reset()`` autouse 每用例清空连接表。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api import task_registry
from src.api.deps import get_chapter_service
from src.api.main import create_app
from src.api.routes import websocket as ws_route
from src.api.websocket.channels import (
    chapter_events_channel,
    task_progress_channel,
)
from src.api.websocket.manager import _build_message, manager
from src.config import settings
from src.core.agents import _prompt as prompt_module
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.services.chapter_service import ChapterService
from src.services.workspace_service import WorkspaceService


# =============================================================================
# 公共辅助
# =============================================================================


def _wait_for_subscription(channel: str, timeout: float = 2.0) -> None:
    """轮询 manager 内部连接表,直到至少一个连接订阅了 ``channel``。

    TestClient WebSocket 没有 sub-ack 机制(spec 不要求);用 manager
    内部状态轮询规避"send subscribe → publish"竞态。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for conn in manager._connections.values():
            if channel in conn.channels:
                return
        time.sleep(0.01)
    raise TimeoutError(f"等订阅 {channel} 超时")


def _wait_for_unsubscription(channel: str, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(channel not in c.channels for c in manager._connections.values()):
            return
        time.sleep(0.01)
    raise TimeoutError(f"等取消订阅 {channel} 超时")


def _wait_for_disconnect(timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not manager._connections:
            return
        time.sleep(0.01)
    raise TimeoutError("等连接清理超时")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_manager() -> Iterator[None]:
    manager.reset()
    yield
    manager.reset()


@pytest.fixture()
def app_client() -> Iterator[TestClient]:
    """简版 client(纯 WebSocket 测试用,不绑 ChapterService)。"""
    app = create_app()
    with TestClient(app) as c:
        yield c


# =============================================================================
# 1. 订阅 + 推送
# =============================================================================


class TestSubscribeAndPublish:
    def test_subscribe_then_publish_receives_event(
        self, app_client: TestClient
    ) -> None:
        with app_client.websocket_connect("/api/v1/ws") as ws:
            channel = chapter_events_channel("ch-0001")
            ws.send_json({"type": "subscribe", "channels": [channel]})
            _wait_for_subscription(channel)
            manager.publish(
                channel=channel,
                event="state_transition",
                data={"from": "drafting", "to": "reviewing"},
            )
            msg = ws.receive_json()
            assert msg["channel"] == channel
            assert msg["event"] == "state_transition"
            assert msg["data"] == {"from": "drafting", "to": "reviewing"}
            assert msg["timestamp"].endswith("Z")

    def test_unsubscribe_stops_delivery(
        self, app_client: TestClient
    ) -> None:
        with app_client.websocket_connect("/api/v1/ws") as ws:
            ch_a = chapter_events_channel("ch-A")
            ch_b = chapter_events_channel("ch-B")
            ws.send_json({"type": "subscribe", "channels": [ch_a, ch_b]})
            _wait_for_subscription(ch_a)
            _wait_for_subscription(ch_b)
            ws.send_json({"type": "unsubscribe", "channels": [ch_a]})
            _wait_for_unsubscription(ch_a)

            # ch_a 推送被丢弃,只有 ch_b 到达
            manager.publish(ch_a, "state_transition", {"from": "x", "to": "y"})
            manager.publish(ch_b, "state_transition", {"from": "p", "to": "q"})
            msg = ws.receive_json()
            assert msg["channel"] == ch_b

    def test_multi_channel_subscription(
        self, app_client: TestClient
    ) -> None:
        with app_client.websocket_connect("/api/v1/ws") as ws:
            ch_a = chapter_events_channel("ch-A")
            ch_b = task_progress_channel("task_xyz")
            ws.send_json({"type": "subscribe", "channels": [ch_a, ch_b]})
            _wait_for_subscription(ch_a)
            _wait_for_subscription(ch_b)
            manager.publish(ch_a, "state_transition", {"from": "a", "to": "b"})
            manager.publish(ch_b, "stage", {"stage": "writing", "progress": 0.5})
            msg1 = ws.receive_json()
            msg2 = ws.receive_json()
            assert {msg1["channel"], msg2["channel"]} == {ch_a, ch_b}

    def test_unknown_message_type_silently_ignored(
        self, app_client: TestClient
    ) -> None:
        with app_client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "garbage", "k": "v"})
            ch = chapter_events_channel("ch-0001")
            ws.send_json({"type": "subscribe", "channels": [ch]})
            _wait_for_subscription(ch)
            manager.publish(ch, "state_transition", {"from": "a", "to": "b"})
            msg = ws.receive_json()
            assert msg["channel"] == ch


# =============================================================================
# 2. 连接断开清理
# =============================================================================


class TestDisconnect:
    def test_disconnect_removes_connection(
        self, app_client: TestClient
    ) -> None:
        with app_client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json(
                {
                    "type": "subscribe",
                    "channels": [chapter_events_channel("ch-1")],
                }
            )
            _wait_for_subscription(chapter_events_channel("ch-1"))
            assert len(manager._connections) == 1
        # with 退出 → WebSocketDisconnect → 服务端 finally 清理
        _wait_for_disconnect()
        assert len(manager._connections) == 0


# =============================================================================
# 3. 心跳
# =============================================================================


class TestHeartbeat:
    def test_pong_keeps_connection_alive(
        self,
        app_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ws_route, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(ws_route, "HEARTBEAT_MAX_MISSED", 2)
        with app_client.websocket_connect("/api/v1/ws") as ws:
            msg = ws.receive_json()
            assert msg == {"type": "ping"}
            ws.send_json({"type": "pong"})
            # 第二条 ping 说明连接还活着(回 pong 清零 missed)
            msg = ws.receive_json()
            assert msg == {"type": "ping"}

    def test_no_pong_closes_connection(
        self,
        app_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ws_route, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(ws_route, "HEARTBEAT_MAX_MISSED", 1)
        with app_client.websocket_connect("/api/v1/ws") as ws:
            with pytest.raises(WebSocketDisconnect):
                # 不停接收;客户端不回 pong → 服务端两轮后 close
                for _ in range(50):
                    ws.receive_json()


# =============================================================================
# 4. 章节路由端到端 → 触发 state_transition
# =============================================================================


@pytest.fixture()
def workspaces_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture()
def archives_root(tmp_path: Path) -> Path:
    return tmp_path / "archives"


@pytest.fixture()
def workspace_service(
    workspaces_root: Path, archives_root: Path
) -> WorkspaceService:
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
    p = tmp_path / "prompts_root"
    p.mkdir()
    (p / "writer.md").write_text("FAKE_WRITER", encoding="utf-8")
    (p / "reviewer.md").write_text("FAKE_REVIEWER", encoding="utf-8")
    (p / "reviser.md").write_text("FAKE_REVISER", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _patch_prompts_dir(
    monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path
) -> None:
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


def _continuity() -> ContinuityContext:
    return ContinuityContext(
        prev_chapter_tail="他握紧剑柄,目光不离对面那道身影。",
        unfinished_actions=[],  # P4-B-07:衔接桥默认通过
        time_gap="立即续接",
        scene_continuation="switch",  # P4-B-07:跳过 scene_continuation_location
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )


def _card(chapter_id: str = "ch-0002") -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章",
        tension_level=8,
        # P4-B-06:validator 默认放宽,本文件测 WebSocket,validator 不参与。
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=3,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="青崖山",
        continuity_context=_continuity(),
        next_chapter_seed="玉佩",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["t1"],
        related_characters=["林衍"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )


def _writer_resp(content: str = "正文") -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=100, output_tokens=200),
        finish_reason="end_turn",
    )


def _reviewer_resp(score: float = 8.0) -> LLMResponse:
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
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=100, output_tokens=200),
        finish_reason="end_turn",
    )


class TestRouteEndToEnd:
    def test_write_route_publishes_state_transitions(
        self,
        workspace_service: WorkspaceService,
        chapter_service: ChapterService,
        mock_client: MagicMock,
    ) -> None:
        """``POST /chapters/{id}:write`` 端到端:通过审稿一次性 pass 路径,
        DRAFTING → REVIEWING(draft_done)→ APPROVED(verdict_pass),
        应至少收到两条 state_transition 事件。
        """
        workspace_service.create(
            "ws-demo",
            display_name="x",
            genre="scifi",
            premise="y",
        )
        chapter_service.save_chapter_card("ws-demo", _card())
        mock_client.complete.side_effect = [
            _writer_resp("v1"),
            _reviewer_resp(score=8.0),
        ]

        app = create_app()
        app.dependency_overrides[get_chapter_service] = lambda: chapter_service
        try:
            with TestClient(app) as client:
                with client.websocket_connect("/api/v1/ws") as ws:
                    channel = chapter_events_channel("ch-0002")
                    ws.send_json({"type": "subscribe", "channels": [channel]})
                    _wait_for_subscription(channel)

                    resp = client.post(
                        "/api/v1/chapters/ch-0002:write",
                        headers={"X-Workspace-Id": "ws-demo"},
                    )
                    # v1.0.x Commit B::write 改 BackgroundTasks 异步,202 +
                    # TaskInfo(pending);state_transition 事件仍由 service
                    # 内 on_state_transition callback 推送到 chapter events
                    # 频道(独立于 task progress 频道),前端 ChapterEditor 现有
                    # 订阅路径不变。
                    assert resp.status_code == 202

                    events: list[dict] = []
                    for _ in range(5):
                        msg = ws.receive_json()
                        if msg.get("event") == "state_transition":
                            events.append(msg)
                        if len(events) >= 2:
                            break

                    assert len(events) >= 2
                    assert events[0]["data"] == {"from": "drafting", "to": "reviewing"}
                    assert events[-1]["data"]["to"] == "approved"
        finally:
            app.dependency_overrides.clear()


# =============================================================================
# 5. 频道命名
# =============================================================================


class TestChannelNaming:
    def test_chapter_events_channel_format(self) -> None:
        assert chapter_events_channel("ch-0002") == "chapter.ch-0002.events"

    def test_task_progress_channel_format(self) -> None:
        assert (
            task_progress_channel("task_abc123") == "task.task_abc123.progress"
        )


# =============================================================================
# 6. Manager 单元
# =============================================================================


class TestManagerUnit:
    def test_publish_to_no_subscriber_is_noop(self) -> None:
        manager.publish(
            "chapter.x.events", "state_transition", {"from": "a", "to": "b"}
        )

    def test_subscribe_unknown_conn_is_silent(self) -> None:
        manager.subscribe("conn_does_not_exist", ["any.channel"])
        manager.unsubscribe("conn_does_not_exist", ["any.channel"])
        assert manager.channels_for("conn_does_not_exist") == set()

    def test_message_format_matches_spec(self) -> None:
        """API_CONTRACT §5.2:服务端推送格式必含 channel/event/timestamp/data。"""
        msg = _build_message("chapter.x.events", "state_transition", {"k": "v"})
        assert set(msg.keys()) == {"channel", "event", "timestamp", "data"}
        assert msg["channel"] == "chapter.x.events"
        assert msg["event"] == "state_transition"
        assert msg["data"] == {"k": "v"}
        assert msg["timestamp"].endswith("Z")
