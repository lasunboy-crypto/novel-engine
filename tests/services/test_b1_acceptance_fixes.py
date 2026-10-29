"""v1.1 wave 4 B1 acceptance 根因修复回归测试(S2 + Y1)。

- S2:``_find_prev_chapter_id`` 必须用全局 ``chapter_id`` 推导上一章,**不被
  卷内 ``chapter_number`` 跨卷重复迷惑**(B1 ch-5 撞 ch-16 现场)。
- Y1:SQLite 连接必须开 ``WAL`` + ``busy_timeout``(根治后台并发写 database
  is locked)。

均不依赖真 LLM,纯 tmp_path。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text

from src.cli.commands import make_db_session
from src.core.db.models import ChapterORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService


def _write_card(paths: WorkspacePaths, chapter_id: str, chapter_number: int) -> None:
    (paths.cards_dir / f"{chapter_id}.json").write_text(
        json.dumps({"chapter_id": chapter_id, "chapter_number": chapter_number}),
        encoding="utf-8",
    )


class TestS2FindPrevChapterIdGlobal:
    """S2:上一章推导走全局 chapter_id,不用卷内 chapter_number 作时序锚。"""

    def _svc(self) -> ChapterService:
        # _find_prev_chapter_id 仅用 paths + 入参(无 self 状态)→ __new__ 即可。
        return ChapterService.__new__(ChapterService)

    def test_cross_volume_boundary_resolves_global_prev(self, tmp_path) -> None:
        paths = WorkspacePaths("ws-s2", workspaces_root=tmp_path)
        paths.cards_dir.mkdir(parents=True, exist_ok=True)
        # 模拟多卷:卷1末 ch-0006(卷内6)、卷2首 ch-0007(卷内1);诱饵 ch-0001
        # (卷内1,与 ch-0007 同卷内号)——旧实现会按 chapter_number 误命中。
        _write_card(paths, "ch-0001", 1)
        _write_card(paths, "ch-0006", 6)
        _write_card(paths, "ch-0007", 1)
        svc = self._svc()
        # ch-0007 上一章 = ch-0006(全局-1),绝非同卷内号的 ch-0001。
        assert svc._find_prev_chapter_id(paths, "ch-0007") == "ch-0006"
        # ch-0006 的全局上一章 = ch-0005,但 ch-0005.json 不存在 → None。
        assert svc._find_prev_chapter_id(paths, "ch-0006") is None

    def test_first_chapter_and_bad_id_return_none(self, tmp_path) -> None:
        paths = WorkspacePaths("ws-s2b", workspaces_root=tmp_path)
        paths.cards_dir.mkdir(parents=True, exist_ok=True)
        _write_card(paths, "ch-0001", 1)
        svc = self._svc()
        assert svc._find_prev_chapter_id(paths, "ch-0001") is None  # 首章
        assert svc._find_prev_chapter_id(paths, "ch-0099") is None  # prev 不存在
        assert svc._find_prev_chapter_id(paths, "not-a-chapter") is None  # 非法格式


class TestY1SqliteWalPragma:
    """Y1:WAL + busy_timeout + cost_tracker 进程内串行锁治本(并发写同 chapter row)。"""

    def test_wal_and_busy_timeout_enabled(self, tmp_path) -> None:
        paths = WorkspacePaths("ws-wal", workspaces_root=tmp_path)
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        session = make_db_session(paths)
        try:
            assert session.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert session.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        finally:
            session.close()

    def test_concurrent_recorder_no_lock_no_lost_update(self, tmp_path) -> None:
        """Y1 治本回归:并发跑真实 cost recorder 写同一 chapter row。

        recorder 是 ``SELECT…UPDATE`` read-modify-write;多线程并发在 WAL 下
        会撞 SQLITE_BUSY_SNAPSHOT(立即 locked,busy_timeout 不救)+ lost-update。
        ``_COST_WRITE_LOCK`` 进程内串行后 → 全成功、cost/token 累加精确无丢。
        """
        from src.services.cost_tracker import make_chapter_recorder

        paths = WorkspacePaths("ws-y1c", workspaces_root=tmp_path)
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        seed = make_db_session(paths)
        seed.add(
            ChapterORM(
                chapter_id="ch-0001",
                workspace_id="ws-y1c",
                current_version=1,
                status="drafting",
                created_at=datetime.now(timezone.utc),
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        seed.commit()
        seed.close()

        recorder = make_chapter_recorder(
            workspace_id="ws-y1c",
            chapter_id="ch-0001",
            session_factory=lambda: make_db_session(paths),
        )
        # 轻量 stub:recorder 只读 profile.profile_id / response.model_used /
        # response.usage.{input,output}_tokens —— 无需构造完整 Pydantic 模型。
        profile = SimpleNamespace(profile_id="p-test", cost_currency="USD")
        n_writers = 12
        errors: list[str] = []

        def _call() -> None:
            try:
                resp = SimpleNamespace(
                    model_used="qwen3.7-max",
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                )
                recorder(profile, resp, 1.0)
            except Exception as exc:  # noqa: BLE001 - 测试捕获并发异常
                errors.append(repr(exc))

        threads = [threading.Thread(target=_call) for _ in range(n_writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"并发 recorder 出现异常(Y1 复发):{errors}"
        check = make_db_session(paths)
        try:
            row = check.get(ChapterORM, "ch-0001")
            assert row.total_cost_usd == float(n_writers), (
                f"cost lost-update:期望 {n_writers},实得 {row.total_cost_usd}"
            )
            bucket = row.total_token_usage["qwen3.7-max"]
            assert bucket["input_tokens"] == 10 * n_writers, bucket
            assert bucket["output_tokens"] == 5 * n_writers, bucket
        finally:
            check.close()

    def test_cost_write_retries_on_lock_then_succeeds(self, tmp_path) -> None:
        """Y1 缺陷 C 治本回归:cost 写撞瞬时 ``database is locked`` 时重试到成功。

        模拟 cost-vs-extract 并发(extract persist 短暂占写锁):前两次
        ``_record_chapter_cost_once`` 抛 lock 错误,第三次放行真写。retry-on-lock
        须吞掉前两次、最终**精确写入**,recorder 不上抛(否则成本统计静默丢失)。
        """
        from unittest.mock import patch

        from sqlalchemy.exc import OperationalError

        from src.services import cost_tracker as ct

        paths = WorkspacePaths("ws-y1c-retry", workspaces_root=tmp_path)
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        seed = make_db_session(paths)
        seed.add(
            ChapterORM(
                chapter_id="ch-0001",
                workspace_id="ws-y1c-retry",
                current_version=1,
                status="drafting",
                created_at=datetime.now(timezone.utc),
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        seed.commit()
        seed.close()

        recorder = ct.make_chapter_recorder(
            workspace_id="ws-y1c-retry",
            chapter_id="ch-0001",
            session_factory=lambda: make_db_session(paths),
        )
        profile = SimpleNamespace(profile_id="p-test", cost_currency="USD")
        resp = SimpleNamespace(
            model_used="qwen3.7-max",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

        real_once = ct._record_chapter_cost_once
        calls = {"n": 0}

        def _flaky_once(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OperationalError(
                    "UPDATE chapters", {}, Exception("database is locked")
                )
            return real_once(**kwargs)

        with patch.object(ct, "_record_chapter_cost_once", side_effect=_flaky_once):
            recorder(profile, resp, 1.0)  # 不应上抛

        assert calls["n"] == 3, f"应重试到第3次成功,实际 {calls['n']}"
        check = make_db_session(paths)
        try:
            row = check.get(ChapterORM, "ch-0001")
            assert row.total_cost_usd == 1.0, row.total_cost_usd
            bucket = row.total_token_usage["qwen3.7-max"]
            assert bucket["input_tokens"] == 10, bucket
            assert bucket["output_tokens"] == 5, bucket
        finally:
            check.close()

    def test_cost_write_non_lock_error_not_retried(self, tmp_path) -> None:
        """非 lock 类 ``OperationalError`` 立即上抛,不被重试掩盖(防吞真错误)。"""
        from unittest.mock import patch

        import pytest
        from sqlalchemy.exc import OperationalError

        from src.services import cost_tracker as ct

        paths = WorkspacePaths("ws-y1c-nonlock", workspaces_root=tmp_path)
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        recorder = ct.make_chapter_recorder(
            workspace_id="ws-y1c-nonlock",
            chapter_id="ch-0001",
            session_factory=lambda: make_db_session(paths),
        )
        profile = SimpleNamespace(profile_id="p-test", cost_currency="USD")
        resp = SimpleNamespace(
            model_used="qwen3.7-max",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

        calls = {"n": 0}

        def _boom_once(**kwargs):
            calls["n"] += 1
            raise OperationalError(
                "UPDATE", {}, Exception("no such column: foo")
            )

        with patch.object(ct, "_record_chapter_cost_once", side_effect=_boom_once):
            with pytest.raises(OperationalError):
                recorder(profile, resp, 1.0)
        assert calls["n"] == 1, "非 lock 错误不应重试"


class TestS3TimelineOrderGlobalBase:
    """S3:_persist_timeline 用全局章序硬注入 time_order 基数,跨卷不碰撞(S2 同源根治)。"""

    def test_cross_volume_no_time_order_collision(self, tmp_path) -> None:
        from src.core.agents.memory_extractor import (
            ExtractedTimelineEntry,
            _persist_timeline,
        )

        paths = WorkspacePaths("ws-s3", workspaces_root=tmp_path)
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        session = make_db_session(paths)
        try:

            def _items(prefix: str) -> list:
                # 模拟 LLM 给的卷内碰撞基数:ch-1 与 ch-7 都给 101/102(卷内章号*100)。
                return [
                    ExtractedTimelineEntry(
                        entry_id=f"{prefix}-e1",
                        description="事件一",
                        time_label="本章",
                        time_order=101,
                    ),
                    ExtractedTimelineEntry(
                        entry_id=f"{prefix}-e2",
                        description="事件二",
                        time_label="本章",
                        time_order=102,
                    ),
                ]

            card1 = SimpleNamespace(chapter_id="ch-0001")  # 卷一第1章
            card7 = SimpleNamespace(chapter_id="ch-0007")  # 卷二第1章(卷内号同为1)
            saved1 = _persist_timeline(
                session, _items("a"), card=card1, workspace_id="ws-s3"
            )
            saved7 = _persist_timeline(
                session, _items("b"), card=card7, workspace_id="ws-s3"
            )
            to1 = sorted(e.time_order for e in saved1)
            to7 = sorted(e.time_order for e in saved7)
            # 全局章序*100 + 章内rank:ch-1→[101,102];ch-7→[701,702](不再撞 ch-1)。
            assert to1 == [101, 102], to1
            assert to7 == [701, 702], to7
            assert set(to1).isdisjoint(to7), "跨卷 time_order 不应碰撞(S3 根治)"
        finally:
            session.close()


class TestPlotThreadPlantedChapterEarliest:
    """rewind 链路缺陷治本:plot_thread planted_chapter 早优先,update 不被后续章覆盖。

    根因:长伏笔(ch-2 引入、ch-7 才落库)的 planted_chapter 若被后续章 update 覆盖
    成 ch-7 → rewind ch-7 按 planted==ch-7 误 revoke 该长伏笔 → 重跑 reference gate
    判 unknown 死锁(luo-clean-v2 ch-7 rewind 现场)。对齐 character first_appearance_chapter
    早优先合并。
    """

    def test_planted_chapter_not_overwritten_by_later_chapter(self, tmp_path) -> None:
        from src.core.agents.memory_extractor import (
            ExtractedPlotThread,
            _persist_plot_threads,
        )
        from src.core.memory.store import get_plot_thread

        paths = WorkspacePaths("ws-s4", workspaces_root=tmp_path)
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        session = make_db_session(paths)
        try:

            def _thread(planted: str) -> "ExtractedPlotThread":
                return ExtractedPlotThread(
                    thread_id="thread-x-long-foreshadow",
                    title="长伏笔",
                    status="planted",
                    importance=4,
                    planted_chapter=planted,
                    description="ch-2 引入,贯穿全书",
                )

            # ch-2 首次引入(planted=ch-0002)
            _persist_plot_threads(
                session,
                [_thread("ch-0002")],
                card=SimpleNamespace(chapter_id="ch-0002"),
                workspace_id="ws-s4",
            )
            session.commit()
            # ch-7 再次提及,LLM 给晚的 planted_chapter=ch-0007
            _persist_plot_threads(
                session,
                [_thread("ch-0007")],
                card=SimpleNamespace(chapter_id="ch-0007"),
                workspace_id="ws-s4",
            )
            session.commit()

            t = get_plot_thread(session, "thread-x-long-foreshadow")
            assert t is not None
            # 早优先:planted_chapter 保留 ch-0002(不被 ch-7 覆盖)→ rewind ch-7 不误伤
            assert t.planted_chapter == "ch-0002", t.planted_chapter
            # last_updated_chapter 仍更新(证明 update 生效,只是 planted 不覆盖)
            assert t.last_updated_chapter == "ch-0007"
        finally:
            session.close()

    def test_update_revives_soft_revoked_plot_thread(self, tmp_path) -> None:
        """rewind 重跑复活语义:被 soft-revoke 的 thread,重跑 update 后 revoked_at 清空。"""
        from src.core.agents.memory_extractor import (
            ExtractedPlotThread,
            _persist_plot_threads,
        )
        from src.core.db.models import PlotThreadORM

        paths = WorkspacePaths("ws-revive", workspaces_root=tmp_path)
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        session = make_db_session(paths)
        try:
            item = ExtractedPlotThread(
                thread_id="t-revive",
                title="伏笔",
                status="planted",
                importance=3,
                planted_chapter="ch-0002",
                description="d",
            )
            _persist_plot_threads(
                session,
                [item],
                card=SimpleNamespace(chapter_id="ch-0002"),
                workspace_id="ws-revive",
            )
            session.commit()
            # 模拟 rewind soft-revoke
            session.get(PlotThreadORM, "t-revive").revoked_at = datetime.now(timezone.utc)
            session.commit()
            assert session.get(PlotThreadORM, "t-revive").revoked_at is not None
            # 重跑 update → 复活(revoked_at 清空),否则重跑记忆被困不可见
            _persist_plot_threads(
                session,
                [item],
                card=SimpleNamespace(chapter_id="ch-0002"),
                workspace_id="ws-revive",
            )
            session.commit()
            assert session.get(PlotThreadORM, "t-revive").revoked_at is None
        finally:
            session.close()


class TestVectorStoreUpsert:
    """缺陷 B-2:VectorStore.add 用 upsert,同 id 覆盖而非 DuplicateIDError(rewind 重跑)。"""

    def test_add_same_id_upserts_not_duplicate(self, tmp_path) -> None:
        from src.core.memory.vector import VectorStore

        vs = VectorStore(
            "ws-upsert",
            embedding_fn=lambda docs: [[0.1, 0.2, 0.3, 0.4] for _ in docs],
            persist_dir=tmp_path / "chroma",
        )
        vs.add("memories", ids=["e1"], documents=["原始"], metadatas=[{"revoked": True}])
        # 同 id 再写(模拟 rewind 软删后重跑 extract)→ upsert 覆盖,不抛 DuplicateIDError
        vs.add("memories", ids=["e1"], documents=["重跑覆盖"], metadatas=[{"k": "v"}])
        assert vs.count("memories") == 1  # 覆盖不翻倍

    def test_persist_to_vector_store_dedups_duplicate_entity(self, tmp_path) -> None:
        """缺陷 B-3:LLM extract 重复输出同 entity → _persist_to_vector_store 单批去重,
        不抛 DuplicateIDError(chroma upsert 单次调用要求 ids 唯一)。"""
        from src.core.agents.memory_extractor import _persist_to_vector_store
        from src.core.memory.vector import VectorStore

        vs = VectorStore(
            "ws-dedup",
            embedding_fn=lambda docs: [[0.1, 0.2, 0.3, 0.4] for _ in docs],
            persist_dir=tmp_path / "chroma",
        )
        dup = SimpleNamespace(
            thread_id="t-dup",
            title="重复伏笔",
            description="LLM 输出两条",
            status="planted",
            importance=3,
            planted_chapter="ch-0001",
            planned_payoff=None,
            actual_payoff=None,
            related_characters=[],
        )
        # 两条相同 thread_id(模拟 LLM 重复输出)→ 单批 ids 重复
        _persist_to_vector_store(
            vector_store=vs,
            characters=[],
            plot_threads=[dup, dup],
            timeline=[],
            facts=[],
            card=SimpleNamespace(chapter_id="ch-0001"),
            workspace_id="ws-dedup",
        )
        assert vs.count("memories") == 1  # 去重后 1 条,不抛 DuplicateIDError


class TestTimelineListExcludesRevoked:
    """timeline_monotonic 预读默认过滤 revoked(B1 长跑 ch-9+ 误报根治)。

    rewind 章后台 soft-revoke 的旧 timeline entry(``revoked_at`` 非空,且
    ``time_order`` 停在 S3 修复前的旧值)若被 ``list_timeline_entries_by_workspace``
    预读出来喂 ``check_timeline_monotonic``,会与重跑后的 active 条目 ``time_order``
    碰撞 → 每章刷 ``timeline_monotonic_warn_only`` 误报(active 数据本身严格单调)。
    ``validator_report_service`` 早已显式 ``revoked_at IS NULL`` 过滤(对的范本),
    本回归锁定 store 函数默认对齐之。
    """

    def test_list_excludes_revoked_by_default(self, tmp_path) -> None:
        from datetime import datetime, timezone

        from src.core.agents.memory_extractor import (
            ExtractedTimelineEntry,
            _persist_timeline,
        )
        from src.core.db.models import TimelineEntryORM
        from src.core.memory.store import list_timeline_entries_by_workspace

        paths = WorkspacePaths("ws-tlrev", workspaces_root=tmp_path)
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        session = make_db_session(paths)
        try:
            saved = _persist_timeline(
                session,
                [
                    ExtractedTimelineEntry(
                        entry_id="e1",
                        description="事件一",
                        time_label="本章",
                        time_order=101,
                    ),
                    ExtractedTimelineEntry(
                        entry_id="e2",
                        description="事件二",
                        time_label="本章",
                        time_order=102,
                    ),
                ],
                card=SimpleNamespace(chapter_id="ch-0001"),
                workspace_id="ws-tlrev",
            )
            assert len(saved) == 2
            # soft-revoke 第一条(模拟 rewind 软删除)
            revoked_id = saved[0].entry_id
            row = session.get(TimelineEntryORM, revoked_id)
            row.revoked_at = datetime.now(timezone.utc)
            session.commit()

            # 默认:排除 revoked
            default = list_timeline_entries_by_workspace(session, "ws-tlrev")
            default_ids = {e.entry_id for e in default}
            assert revoked_id not in default_ids, default_ids
            assert len(default) == 1, default_ids

            # include_revoked=True:返回全部(审计场景)
            all_rows = list_timeline_entries_by_workspace(
                session, "ws-tlrev", include_revoked=True
            )
            assert len(all_rows) == 2
            assert revoked_id in {e.entry_id for e in all_rows}
        finally:
            session.close()
