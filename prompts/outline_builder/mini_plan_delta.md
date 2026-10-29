# 单卷章级创意增量（Hy3 strict schema）

你负责每章实际事件、信息增量、参与人物/伏笔、回响、结构焦点、人物弧光台阶、互动中心、升级落点，以及是否由该章承担已有 typed dramatic beat。程序按数组顺序从卷范围注入 chapter/volume/arc 稳定 ID 与章号，并逐字段复制所选 dramatic contract，组装现有 `MiniPlanOutput`。

严格遵守 Provider JSON Schema，只输出 JSON。不要输出 `chapter_id`、`chapter_number`、`volume_id`、`arc_id` 或完整 dramatic contract。

- `items` 数量必须精确等于本卷冻结章数；第 N 项就是卷内第 N 章。
- 人物、伏笔、callback 和 `dramatic_beat_id` 只能选择上游已有 ID；列表不得重复。
- `arc_beats` 使用 bounded key/value list；有真实人物变化才填，普通章明确输出空数组。
- `chapter_focus`、`interaction_focus`、`breakthrough_payoff` 允许空字符串；空值是创作留白，不按配额补齐。
- `one_line_event` 必须是本章独有的具体事件；程序不会生成冲突、动作、场景、情绪、对话或钩子。

缺字段、错误引用、同章多 arc 或所选 beat 不覆盖本章都会 fail closed。
