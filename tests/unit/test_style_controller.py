"""M1 动态卡控制器防回退测试(v1.1 wave 5 session 1)。

覆盖详细设计 ``docs/plan/m1_dynamic_card_controller_design.md`` §7 的 4+2 防回退测试方案:

1. 强化触发(窗口内 flag ≥ TH_ADD → 加入 avoid-list)
2. 弱化撤销(连续 M_REMOVE 章干净 → 撤出)
3. 滞后不震荡(flag→clean→flag 不抖,撤出/重加都需越过不对称阈值)
4. 卡有界(> CAP 活跃套话 → 截断到 CAP + warn)
5. 持久化往返(record 写盘 → load 读盘一致)
6. 空 violations → 全已知短语 clean_streak +1(不误锁)

外加(实现期工程判断防回退):
7. record 对同一 chapter_seq 幂等(orchestrator rewrite 重审同章不重复计数 / 推进)
8. ``_build_forbidden_style_block`` 空 / 非空渲染(零回归 + banner 措辞护留白)
9. 缺台账文件 → avoid-list 空(老工作区零回归 / fail-safe)

全 mock 确定性,不调真 LLM(信号 violations 直接喂)。
"""

from __future__ import annotations

from pathlib import Path

from structlog.testing import capture_logs

from src.core.style_controller import (
    DEFAULT_PARAMS,
    StyleControllerParams,
    active_avoid_list,
    load_ledger,
    record,
)


# ---- 默认参数自检(防参数漂移破坏测试假设) --------------------------------
def test_default_params_match_design() -> None:
    """设计 §6 起始值:K=5 / TH_ADD=2 / M_REMOVE=3 / CAP=10。"""
    assert DEFAULT_PARAMS.window == 5
    assert DEFAULT_PARAMS.threshold_add == 2
    assert DEFAULT_PARAMS.streak_remove == 3
    assert DEFAULT_PARAMS.cap == 10


# ---- 1. 强化触发 ----------------------------------------------------------
def test_reinforce_triggers_active_at_threshold(tmp_path: Path) -> None:
    """窗口内被 flag 达 TH_ADD(2)次 → active;active_avoid_list 含该套话。"""
    record(tmp_path, 2, ["毛骨悚然"])
    # 一次 flag(count=1 < 2)还不该激活
    assert active_avoid_list(tmp_path) == []

    record(tmp_path, 3, ["毛骨悚然"])
    # 窗口内第 2 次 flag → 激活
    assert active_avoid_list(tmp_path) == ["毛骨悚然"]
    ledger = load_ledger(tmp_path)
    assert ledger.phrases["毛骨悚然"].active is True


def test_reinforce_needs_within_window(tmp_path: Path) -> None:
    """两次 flag 间隔超出窗口(K=5)→ 旧 flag 被裁剪,count 不累计到阈值。"""
    record(tmp_path, 2, ["逆时针跳动"])
    # 章 2 的 flag 在章 8 时已滑出 [4,8] 窗口
    record(tmp_path, 8, ["逆时针跳动"])
    assert active_avoid_list(tmp_path) == []
    ledger = load_ledger(tmp_path)
    # 只剩窗口内章 8 这一次
    assert ledger.phrases["逆时针跳动"].flagged_chapters == [8]


# ---- 2. 弱化撤销 ----------------------------------------------------------
def test_decay_removes_after_clean_streak(tmp_path: Path) -> None:
    """active 套话连续 M_REMOVE(3)章干净 → 撤出 avoid-list。"""
    record(tmp_path, 2, ["猛地睁开眼"])
    record(tmp_path, 3, ["猛地睁开眼"])  # → active
    assert active_avoid_list(tmp_path) == ["猛地睁开眼"]

    record(tmp_path, 4, [])  # clean 1(streak 1 < 3,仍 active)
    assert active_avoid_list(tmp_path) == ["猛地睁开眼"]
    record(tmp_path, 5, [])  # clean 2
    assert active_avoid_list(tmp_path) == ["猛地睁开眼"]
    record(tmp_path, 6, [])  # clean 3 → 撤出
    assert active_avoid_list(tmp_path) == []
    ledger = load_ledger(tmp_path)
    entry = ledger.phrases.get("猛地睁开眼")
    # 撤出后被修剪遗忘(或残留也必须 inactive)
    assert entry is None or entry.active is False


# ---- 3. 滞后不震荡 --------------------------------------------------------
def test_hysteresis_active_survives_single_clean(tmp_path: Path) -> None:
    """已 active + 单章干净(streak 1 < M_REMOVE)→ 不撤(滞后下沿)。"""
    record(tmp_path, 2, ["撕裂"])
    record(tmp_path, 3, ["撕裂"])  # active
    record(tmp_path, 4, [])  # clean 1
    assert active_avoid_list(tmp_path) == ["撕裂"]  # 未撤
    record(tmp_path, 5, ["撕裂"])  # 又 flag,streak 归零
    assert active_avoid_list(tmp_path) == ["撕裂"]  # 全程未抖


def test_hysteresis_single_reflag_after_decay_does_not_reactivate(
    tmp_path: Path,
) -> None:
    """已撤出的套话单次 re-flag(count 1 < TH_ADD)→ 不重新激活(滞后上沿)。"""
    # 先激活再撤出
    record(tmp_path, 2, ["破空"])
    record(tmp_path, 3, ["破空"])
    record(tmp_path, 4, [])
    record(tmp_path, 5, [])
    record(tmp_path, 6, [])  # 撤出
    assert active_avoid_list(tmp_path) == []

    record(tmp_path, 7, ["破空"])  # 单次 re-flag,count 1 < 2
    assert active_avoid_list(tmp_path) == []  # 未重新激活


# ---- 4. 卡有界 ------------------------------------------------------------
def test_cap_bounds_avoid_list_with_warn(tmp_path: Path) -> None:
    """活跃套话 > CAP(10)→ active_avoid_list 截到 CAP + 落 style_phrases_truncated warn。"""
    phrases = [f"套话{i:02d}" for i in range(12)]  # 12 > CAP
    record(tmp_path, 2, phrases)
    record(tmp_path, 3, phrases)  # 每个 count=2 → 全 active(12 个)

    with capture_logs() as logs:
        result = active_avoid_list(tmp_path, workspace_id="ws-cap")
    assert len(result) == DEFAULT_PARAMS.cap  # 截断到 10
    truncate_logs = [
        log for log in logs if log.get("event") == "style_phrases_truncated"
    ]
    assert len(truncate_logs) == 1
    assert truncate_logs[0]["original_count"] == 12
    assert truncate_logs[0]["kept_count"] == 10


# ---- 5. 持久化往返 --------------------------------------------------------
def test_persistence_round_trip(tmp_path: Path) -> None:
    """record 写盘 → load_ledger 读盘状态一致(独立进程语义下台账可恢复)。"""
    record(tmp_path, 2, ["毛骨悚然", "撕裂"])
    record(tmp_path, 3, ["毛骨悚然"])  # 毛骨悚然 → active;撕裂 clean_streak=1

    # 文件真落盘
    assert (tmp_path / "style_controller.json").is_file()

    reloaded = load_ledger(tmp_path)
    assert reloaded.last_chapter_seq == 3
    assert reloaded.dimension == "dedup"
    assert reloaded.phrases["毛骨悚然"].active is True
    assert reloaded.phrases["毛骨悚然"].flagged_chapters == [2, 3]
    assert reloaded.phrases["撕裂"].clean_streak == 1
    assert reloaded.phrases["撕裂"].active is False


# ---- 6. 空 violations → clean_streak 全 +1 --------------------------------
def test_empty_violations_advances_all_clean_streaks(tmp_path: Path) -> None:
    """空 violations(本章干净)→ 所有已知短语 clean_streak +1,不误锁。"""
    record(tmp_path, 2, ["毛骨悚然", "撕裂"])
    ledger = record(tmp_path, 3, [])  # 本章干净
    assert ledger.phrases["毛骨悚然"].clean_streak == 1
    assert ledger.phrases["撕裂"].clean_streak == 1
    # 没有任何套话因"干净一章"被激活
    assert active_avoid_list(tmp_path) == []


# ---- 7. 幂等(实现期工程判断:orchestrator rewrite 重审同章) ----------------
def test_record_idempotent_on_same_chapter_seq(tmp_path: Path) -> None:
    """同 chapter_seq 多次 record(rewrite 重审)→ flag 不重复计数 + streak 不重复推进。

    orchestrator 主状态机(chapter_orchestrator while 循环)rewrite 时会对同一章多次
    重审 dedup → record 可能被同 seq 多次调用。幂等守卫保证台账不被污染。
    """
    record(tmp_path, 2, ["毛骨悚然"])
    record(tmp_path, 2, ["毛骨悚然"])  # 同 seq 重审 → no-op
    record(tmp_path, 2, ["毛骨悚然"])
    ledger = load_ledger(tmp_path)
    # flagged_chapters 不出现 [2,2,2]
    assert ledger.phrases["毛骨悚然"].flagged_chapters == [2]
    assert ledger.last_chapter_seq == 2
    # 单章 1 次 flag(count 1 < TH_ADD)→ 仍未激活(没因重复调用虚增到阈值)
    assert active_avoid_list(tmp_path) == []

    # 干净章的 clean_streak 也不因重审重复推进
    record(tmp_path, 3, [])
    record(tmp_path, 3, [])  # 同 seq no-op
    ledger = load_ledger(tmp_path)
    assert ledger.phrases["毛骨悚然"].clean_streak == 1  # 不是 2


def test_record_skips_stale_old_chapter(tmp_path: Path) -> None:
    """seq < last_chapter_seq(旧章重跑)→ no-op,不回污染已前进的台账。"""
    record(tmp_path, 5, ["撕裂"])
    before = load_ledger(tmp_path)
    record(tmp_path, 3, ["毛骨悚然"])  # 旧章,应被跳过
    after = load_ledger(tmp_path)
    assert "毛骨悚然" not in after.phrases
    assert after.last_chapter_seq == before.last_chapter_seq == 5


# ---- 8. banner 渲染(护留白措辞) ------------------------------------------
def test_build_forbidden_style_block_empty_and_nonempty() -> None:
    """空 list → 空字符串(零回归);非空 → 含套话 + 护留白措辞。"""
    from src.core.agents.chapter_card_generator import _build_forbidden_style_block

    assert _build_forbidden_style_block([]) == ""

    block = _build_forbidden_style_block(["毛骨悚然", "逆时针跳动"])
    assert "毛骨悚然" in block
    assert "逆时针跳动" in block
    assert "套话避免" in block
    # 护留白:措辞强调"避免机械复用",不是禁止意象出现
    assert "避免机械复用" in block
    assert "留白" in block


# ---- 9. 缺台账文件 → 空(老工作区零回归 / fail-safe) -----------------------
def test_missing_ledger_returns_empty(tmp_path: Path) -> None:
    """工作区无 style_controller.json → avoid-list 空 + 空台账,生成行为与 M1 前等价。"""
    assert active_avoid_list(tmp_path) == []
    ledger = load_ledger(tmp_path)
    assert ledger.phrases == {}
    assert ledger.last_chapter_seq == 0


def test_corrupt_ledger_fails_safe(tmp_path: Path) -> None:
    """台账文件损坏 → warn + 空台账(不抛,不阻断生成)。"""
    (tmp_path / "style_controller.json").write_text("{ 不是合法 json", encoding="utf-8")
    with capture_logs() as logs:
        ledger = load_ledger(tmp_path)
    assert ledger.phrases == {}
    assert any(log.get("event") == "style_controller_ledger_corrupt" for log in logs)


# ---- 参数可注入(原型校准支持) -------------------------------------------
def test_custom_params_threshold(tmp_path: Path) -> None:
    """params 可注入(原型长跑校准用):TH_ADD=3 时两次 flag 不够激活。"""
    params = StyleControllerParams(threshold_add=3)
    record(tmp_path, 2, ["毛骨悚然"], params=params)
    record(tmp_path, 3, ["毛骨悚然"], params=params)
    assert active_avoid_list(tmp_path, params=params) == []  # count 2 < 3
    record(tmp_path, 4, ["毛骨悚然"], params=params)
    assert active_avoid_list(tmp_path, params=params) == ["毛骨悚然"]  # count 3
