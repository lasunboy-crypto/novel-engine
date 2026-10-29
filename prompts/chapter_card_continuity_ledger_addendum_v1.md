# R4 章节卡连续性小账本补充合同

本补充合同同时适用于单章与按 beat 批量出卡，并覆盖旧提示词里“上一章卡计划可作
直接事实”的降级表述。

1. `prev_chapter_summaries` 中带 `source_version_id / source_text_hash /
   ledger_hash / evidence` 的最近一章，是上一章 `final.md` 已验证事实。
   `prev_chapter_summary / prev_unfinished_actions / prev_chapter_tail` 非空时也已由
   系统优先改为该小账本事实。不得用上一张卡的 seed 或 must 替换、弱化这些事实。
2. `continuity_context.unfinished_actions` 必须逐字复用
   `prev_unfinished_actions`，不得删掉不方便承接的即时动作。
3. `continuity_context` 额外输出 `continuity_bridge` 字符串：
   - 首章或没有即时未完动作时可填空串 `""`；
   - 若上一章有即时未完动作，且本章 `time_gap` 不是立即续接，或
     `scene_continuation` 为 `switch/time_jump`，必须先逐字写出每条即时未完
     动作，再说明本章开场如何承接/收束它、之后如何合理进入新时间或场景；
   - 禁止只写“承接上章”“危机解除”等不可核查空话。
4. 正确形状示例：

```json
{
  "continuity_context": {
    "prev_chapter_tail": "系统注入的上一章真实尾部",
    "unfinished_actions": ["镜面渗血仍在扩大"],
    "time_gap": "数小时后",
    "scene_continuation": "switch",
    "continuity_bridge": "镜面渗血仍在扩大：本章开场先写众人封住镜面并撤离现场；确认威胁暂时受控后，时间推进到数小时后并切换至旅馆。",
    "pov_change": false,
    "pov_from": "char-001",
    "pov_to": "char-001"
  }
}
```

缺少该字段仍可解析为草稿，但非首章在批准前会由确定性闸门拒绝；不要依赖默认空值。
