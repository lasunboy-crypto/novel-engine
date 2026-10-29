"""v1.1 wave 4 session 10(D1)prompt 示例卫生回归测试。

防两类回归:

① **产卡 prompt 示例体裁中性化**:`chapter_card_generator.md` 是 creative
   agent —— 示例主题会被模仿进创意
   输出,长篇累积可致文风漂移。示例不得再含硬编码仙侠 ID / 角色名(lin-yan /
   林衍 等),防 bias + 防约定再次扩散到新 prompt(w4-s9 batch_by_beat 即从
   generator 抄了整段仙侠示例)。

② **chapter_card_generator.md JSON 示例 forbidden 无风格污染**:与 G19b
   forbidden 语义边界段保持一致。v6 修了 L80 行内示例却漏了 JSON 输出示例
   (仍含"长句堆叠"/"心理描写超过3段"),w4-s10 补修。本测试解析 JSON 示例块跑
   运行时检测器(不做裸字符串搜索 —— 边界段规则里"长句堆叠"作 ❌ 反例合法出现)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.core.models.chapter_card import detect_forbidden_style_pollution

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# 产卡 prompt:主题会被模仿进创意输出 → 示例须完全体裁中性
_CARD_PROMPTS = ["chapter_card_generator.md"]

# 旧仙侠示例 ID / 角色名(纯示例产物,不该出现在任何 card prompt 文本中;
# 不含"山门"等场景词 —— 那些理论上可能在未来规则文本里合法出现,这里只钉
# 不会被合法引用的纯标识符 / 角色名,保证测试稳健不误伤)。
_BANNED_EXAMPLE_IDS = [
    "lin-yan",
    "shi-jie",
    "yu-pei-zhi-yue",
    "林衍",
    "沈雪",
]

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _read_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _collect_example_forbidden(prompt_text: str) -> list[list[str]]:
    """解析 prompt 里所有 ```json``` 示例块,收集每处 forbidden 数组。

    兼容两种结构:顶层 ``{"forbidden": [...]}``(单卡)与
    ``{"cards": [{"forbidden": [...]}, ...]}``(batch)。
    """
    forbidden_lists: list[list[str]] = []
    for block in _JSON_BLOCK_RE.findall(prompt_text):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue  # 非完整 JSON 的示意块跳过
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("forbidden"), list):
            forbidden_lists.append(payload["forbidden"])
        for card in payload.get("cards", []) or []:
            if isinstance(card, dict) and isinstance(card.get("forbidden"), list):
                forbidden_lists.append(card["forbidden"])
    return forbidden_lists


@pytest.mark.parametrize("prompt_name", _CARD_PROMPTS)
def test_card_prompt_no_xianxia_example_ids(prompt_name: str) -> None:
    """D1 回归:产卡 prompt 不含残留仙侠示例 ID / 角色名。"""
    text = _read_prompt(prompt_name)
    for token in _BANNED_EXAMPLE_IDS:
        assert token not in text, (
            f"{prompt_name} 残留仙侠示例 '{token}'(D1 通用化回归 —— "
            "示例须体裁中性,防 bias 泄进创意输出 + 防约定再扩散)"
        )


@pytest.mark.parametrize("prompt_name", _CARD_PROMPTS)
def test_card_prompt_example_forbidden_no_style_pollution(prompt_name: str) -> None:
    """D1/G19b 回归:产卡 prompt 的 JSON 示例 forbidden 不含风格类约束。

    用运行时 ``detect_forbidden_style_pollution`` 检测真示例(而非裸搜字符串),
    确保示例不会教 LLM 产出会被运行时硬拒的 forbidden。
    """
    text = _read_prompt(prompt_name)
    forbidden_lists = _collect_example_forbidden(text)
    assert forbidden_lists, f"{prompt_name} 未解析到任何 forbidden 示例(结构变化?)"
    for forbidden in forbidden_lists:
        polluted = detect_forbidden_style_pollution(forbidden)
        assert polluted == [], (
            f"{prompt_name} JSON 示例 forbidden 含风格污染 {polluted} —— "
            "与 G19b 边界段矛盾,会教 LLM 产出运行时硬拒的 forbidden"
        )
