"""volume_seam_replay_validator 单元测(v1.1 acceptance 修复1,2026-06-10)。

完全 mock ``LLMClient``;fixture 字面取自 **luo-accept-v1 真实 mini_plan**
(6/7 真重演卷界 + 24/25 干净卷界 — 粗筛阈值标定的同一份 ground truth),
让防回退测试钉死真实现场而非合成文本。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.volume_seam_replay_validator import (
    SEAM_PRESCREEN_BIGRAM_THRESHOLD,
    iter_volume_seams,
    seam_suspicion_score,
    validate_volume_seam_replay,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.outline import MiniPlanItem

# ---- luo-accept-v1 真实字面(2026-06-10 acceptance 现场) --------------------

_CH6_ONE_LINE = (
    "沈舟实地勘察老街废弃防空洞发现其与梦中诏狱结构重合,突遭锦衣卫人格轻微"
    "侵蚀,用审讯手法险些掐死一名无辜流浪汉。"
)
_CH6_ARC_BEAT = (
    "沈舟从旁观梦境的承受者转为必须直面侵蚀威胁的求生者——在防空洞内死死掰开"
    "自己掐住流浪汉脖子的手指,用随身携带的镇纸砸破额头换取清醒。"
)
_CH7_ONE_LINE_REPLAY = (
    "沈舟在防空洞强行压制副人格救下流浪汉,随后在诊所接诊时因应激反应,"
    "无意识使出锦衣卫擒拿术制服了一名突然发狂的暴力病患。"
)
_CH24_ONE_LINE = (
    "梦境中锦衣卫持密信查抄裴府将其当街锁拿;现实中陆铮率特案组突入赵氏集团"
    "顶层,将正销毁文件的赵鹤年死死按倒在会议桌前。"
)
_CH25_ONE_LINE = (
    "赵鹤年在被押解时触发顶层暗门与死士反扑,趁机劫持赶到的苏夜退守霖海最高"
    "地标停机坪,逼迫沈舟独自上楼谈判。"
)


def _item(
    chapter_id: str,
    volume_id: str,
    chapter_number: int,
    one_line: str,
    arc_beats: dict[str, str] | None = None,
) -> MiniPlanItem:
    return MiniPlanItem(
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        volume_id=volume_id,
        arc_id=f"{volume_id}-arc-1",
        one_line_event=one_line,
        info_increment="推进。",
        linked_characters=["shen-zhou"],
        arc_beats=arc_beats or {},
    )


def _make_response(
    *,
    replay: bool,
    confidence: float = 0.9,
    evidence: str = "新卷首章重新解决了前卷已完结的掐人冲突。",
    fenced: bool = False,
) -> LLMResponse:
    body = json.dumps(
        {"replay": replay, "confidence": confidence, "evidence": evidence},
        ensure_ascii=False,
    )
    content = f"```json\n{body}\n```" if fenced else body
    return LLMResponse(
        content=content,
        model_used="qwen-max",
        usage=TokenUsage(input_tokens=400, output_tokens=50),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "volume_seam_replay_validator_v1.md").write_text(
        "FAKE_VOLUME_SEAM_REPLAY_VALIDATOR_PROMPT\n\n用于单测的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response(replay=False)
    return client


class TestPrescreen:
    """粗筛标定防回退:真实语料分数关系不许漂(2026-06-10 标定)。"""

    def test_threshold_constant_locked(self) -> None:
        # 阈值变更须重跑标定(luo-accept-v1 + luo-budget-v1 全 8 卷界)再改这里。
        assert SEAM_PRESCREEN_BIGRAM_THRESHOLD == 0.10

    def test_real_replay_seam_fires(self) -> None:
        prev_text = _CH6_ONE_LINE + ";" + _CH6_ARC_BEAT
        score = seam_suspicion_score(_CH7_ONE_LINE_REPLAY, prev_text)
        assert score >= SEAM_PRESCREEN_BIGRAM_THRESHOLD, (
            f"真重演卷界(标定 0.175)必须触发粗筛,实际 {score:.3f}"
        )

    def test_real_clean_seam_below_threshold(self) -> None:
        score = seam_suspicion_score(_CH25_ONE_LINE, _CH24_ONE_LINE)
        assert score < SEAM_PRESCREEN_BIGRAM_THRESHOLD, (
            f"干净卷界 24/25(标定 0.06)不应触发粗筛,实际 {score:.3f}"
        )

    def test_short_text_scores_zero(self) -> None:
        # 单测 / mock fixture 占位短文本天然绕过粗筛(任一侧 bigram < 10)。
        assert seam_suspicion_score("第一章事件", "第二章事件") == 0.0


class TestIterVolumeSeams:
    def test_two_volumes_one_seam(self) -> None:
        items = [
            _item("ch-0001", "v1", 1, _CH24_ONE_LINE),
            _item("ch-0002", "v1", 2, _CH6_ONE_LINE),
            _item("ch-0003", "v2", 1, _CH25_ONE_LINE),
        ]
        seams = iter_volume_seams(items)
        assert len(seams) == 1
        assert seams[0][0].chapter_id == "ch-0002"
        assert seams[0][1].chapter_id == "ch-0003"

    def test_single_volume_no_seam(self) -> None:
        items = [
            _item("ch-0001", "v1", 1, _CH6_ONE_LINE),
            _item("ch-0002", "v1", 2, _CH7_ONE_LINE_REPLAY),
        ]
        assert iter_volume_seams(items) == []


class TestValidate:
    def _replay_items(self) -> list[MiniPlanItem]:
        """luo-accept-v1 6/7 现场重演 fixture。"""
        return [
            _item(
                "ch-0006", "v1", 6, _CH6_ONE_LINE,
                arc_beats={"shen-zhou": _CH6_ARC_BEAT},
            ),
            _item("ch-0007", "v2", 1, _CH7_ONE_LINE_REPLAY),
        ]

    def test_clean_seams_make_zero_llm_calls(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """粗筛不触发 → 零 LLM 调用(mock 单测 / 干净 commit 零成本)。"""
        items = [
            _item("ch-0024", "v4", 6, _CH24_ONE_LINE),
            _item("ch-0025", "v5", 1, _CH25_ONE_LINE),
        ]
        result = validate_volume_seam_replay(
            mini_plan_items=items,
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.seams_total == 1
        assert result.seams_prescreen_fired == 0
        assert result.llm_calls_count == 0
        mock_client.complete.assert_not_called()

    def test_replay_detected_fails_with_finding(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            replay=True, confidence=0.92
        )
        result = validate_volume_seam_replay(
            mini_plan_items=self._replay_items(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is False
        assert result.seams_prescreen_fired == 1
        assert result.llm_calls_count == 1
        f = result.findings[0]
        assert f.prev_chapter_id == "ch-0006"
        assert f.next_chapter_id == "ch-0007"
        assert f.confidence == 0.92
        # judge 看到的 user message 必须带「已完结的收束」上下文(判定依据)
        user_msg = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "已完结的收束" in user_msg
        assert "掰开自己掐住流浪汉脖子的手指" in user_msg

    def test_judge_says_continuation_passes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """粗筛触发但 judge 判延续(非重演)→ pass(干净话题延续不误杀)。"""
        mock_client.complete.return_value = _make_response(replay=False)
        result = validate_volume_seam_replay(
            mini_plan_items=self._replay_items(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.llm_calls_count == 1
        assert result.findings == []

    def test_code_fenced_json_parsed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            replay=True, fenced=True
        )
        result = validate_volume_seam_replay(
            mini_plan_items=self._replay_items(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is False

    def test_llm_error_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """judge 挂了不阻断 commit(fail-open + warn log)。"""
        mock_client.complete.side_effect = RuntimeError("dashscope 503")
        result = validate_volume_seam_replay(
            mini_plan_items=self._replay_items(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.fail_open_count == 1

    def test_unparseable_mock_output_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """MockLLMClient 类非 JSON 输出 → fail-open(既有 mock commit 测不破)。"""
        mock_client.complete.return_value = LLMResponse(
            content="这是一段大纲正文,不是 JSON。",
            model_used="mock-default",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
            finish_reason="stop",
        )
        result = validate_volume_seam_replay(
            mini_plan_items=self._replay_items(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.fail_open_count == 1

    def test_no_seams_no_setup(self, fake_prompts_dir: Path) -> None:
        """单卷无卷界 → 不触 routing / prompt 读取(client=None 也安全)。"""
        items = [_item("ch-0001", "v1", 1, _CH6_ONE_LINE)]
        result = validate_volume_seam_replay(
            mini_plan_items=items,
            client=None,
            model=None,
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.seams_total == 0
