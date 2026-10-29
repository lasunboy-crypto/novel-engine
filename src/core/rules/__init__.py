"""[HARD] validators(PROJECT_SPEC §4.1 / §4.2 / §4.3 节奏类 / 一致性类 / 衔接类)。

P4-B-06:第一批 4 类节奏类 [HARD] 规则(字数 / must_achieve / must_unresolved /
forbidden)+ ``validator_pipeline`` 串联调度 + Reviewer / Orchestrator 集成钩子。
R9 生命周期拆分:角色存在与已死亡角色引用属于 Writer 前 admission；写后
pipeline 保留正文可判的字数 / 未解 / forbidden / voice / style 等检查。旧
``check_death_foreshadowing`` 名称仅作兼容，真实函数不再冒充“两章死亡铺垫”。

设计要点:
- **无副作用**:validator 函数纯输入纯输出,不读 DB / 不调 LLM(LLM 兜底校验由
  ``validator_pipeline`` 调度,validator 本身不触发);
- **LLM 不可用 vs LLM 异常 二档区分**:
  - ``llm_fallback=None``(LLM 不可用)→ **严格模式**:关键词 miss / 启发式
    hit 直接判失败,与 PROJECT_SPEC §4.1 [HARD] 语义一致;
  - ``llm_fallback`` 调用抛错 → **swallow + 保守通过**:LLM 调用失败不应放
    大成 validator 误判(对齐 P3-B-13 continuity refresh swallow 模式)。
- **stub LLM 接口**:``check_must_achieve`` / ``check_must_unresolved`` 接受可选
  ``llm_fallback`` 回调参数。完整 LLM 兜底实现(prompts / JSON 解析 / 重试 /
  cost 追踪)登入 v2_wishlist,P5+ 落地。
"""

from src.core.rules.validator_pipeline import (
    ValidatorPipelineReport,
    merge_into_review_verdict_overrides,
    run_validator_pipeline,
)
from src.core.rules.validators import (
    WORD_COUNT_ACCEPTANCE_TOLERANCE_RATIO,
    ValidatorResult,
    classify_word_count_acceptance,
    check_character_existence,
    check_dead_character_references,
    check_death_foreshadowing,
    check_forbidden,
    check_must_achieve,
    check_must_unresolved,
    check_scene_continuation_location,
    check_timeline_monotonic,
    check_unfinished_actions_continuation,
    check_voice_forbidden,
    check_word_count,
    word_count_acceptance_bounds,
)

__all__ = [
    "ValidatorPipelineReport",
    "ValidatorResult",
    "WORD_COUNT_ACCEPTANCE_TOLERANCE_RATIO",
    "classify_word_count_acceptance",
    "check_character_existence",
    "check_dead_character_references",
    "check_death_foreshadowing",
    "check_forbidden",
    "check_must_achieve",
    "check_must_unresolved",
    "check_scene_continuation_location",
    "check_timeline_monotonic",
    "check_unfinished_actions_continuation",
    "check_voice_forbidden",
    "check_word_count",
    "word_count_acceptance_bounds",
    "merge_into_review_verdict_overrides",
    "run_validator_pipeline",
]
