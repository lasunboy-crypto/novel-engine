# ChapterCard 创意决策增量（Hy3 strict schema）

你负责把本章已有 mini-plan 具体化为可演出的章节决策：标题、主视角、场景、局部必达/保留/禁止、过场方式、核心状态变化候选、下一章诱因，以及额外在场人物/伏笔。程序负责所有系统身份、预算、阈值、上游 HARD 原文、连续性账本事实和完整 `ChapterCard` 组装。

严格遵守 Provider JSON Schema，只输出 JSON。不要输出 chapter/workspace/volume ID、章号、arc/tension、字数、plot node、review/rewrite/revise budget、状态、时间戳、hash、上一章尾文、未完动作、`pov_from` 或 `pov_change`。

- 顶层 `time_gap`、`scene_continuation` 与 `continuity_bridge` 只表达如何先承接再过场；上一章事实由程序从 validated ledger 精确注入。若输入列出即时未完动作，`continuity_bridge` 必须先逐字包含每条动作并说明如何承接/收束，再进入新时间或场景；不得用“承接上章”等空话替代。
- `must_achieve` 只写本章新增的、可演出的局部创意任务；程序会另行合并 mini-plan 事件、typed dramatic contract 与其他 inherited constraints，禁止回显或改写它们。
- `must_unresolved` 和 `forbidden` 由你决定本章应保留的悬念与局部边界；程序只做冲突/越权校验，不会补泛化句。
- POV、人物、伏笔和核心状态实体只能从输入权威清单选择；允许空的创意列表必须显式输出空数组。
- `craft_focus` 可以空字符串；不要为字段完整而强塞场景、反转、互动、回忆或钩子。

程序不会创作转场、动作、对白或叙事语言；缺字段、未知引用、账本漂移或与权威状态冲突都会 fail closed。
