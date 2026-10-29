# 卷规划创意增量（Hy3 strict schema / range feasibility v1）

你负责每卷标题、概要、戏剧承诺、上游线选择、arc/beat 的叙事功能、剧情推进、must/forbidden、冲突与反作用、人物选择、后果和张力变化。程序只负责冻结事实、必达结构槽、稳定 ID、局部引用、由冻结章数推导的可行数量/区间边界和最终装配；不会替你决定固定桥段、场景、对白或叙事内容。

严格遵守 Provider JSON Schema，只输出 JSON。不要输出程序生成的当前 volume/arc/beat 稳定 ID、chapter_count、tension_template、系统预算、状态或 hash。除 schema 明确给出的 ID/slot 外，不要自行创造引用。

- `volumes` 数量和顺序逐一对应冻结总卷数；每卷 `tension_curve` 长度必须精确等于冻结章数，元素只能是 1–10 整数。程序不会 pad、trim 或 clamp。
- linked 字段只能逐字选择上游已有 ID。单卷没有合法后续卷时，`dramatic_promise.outcome` 只能是 `resolved`，两个 `deferred_to_*` 都必须为 `null`；多卷只允许非末卷选择 schema 给出的后续卷。
- 程序会先从冻结章数和每 arc 最少 beat 数中移除数学上不可能的 arc 数量。当 Schema 用 `prefixItems` / `enum` 冻结 `chapter_range` 时，只能逐字填写给定骨架，不得新增、复制、重叠或改写范围；这只冻结结构坐标，不替你决定剧情。
- Schema 未冻结具体范围时，普通产品每个 arc 仍由你在可行上限内选择 2–4 个 beats；selection_short_arc 路径每个 arc 恰好 1 个 beat。每个 beat 的 summary、must/forbidden 始终由你负责；所有自选范围必须连续、无重叠并覆盖所属 arc。
- `beats` 只写基础创意内容，不再嵌套 `dramatic_contract`。需要额外高价值对抗、转折或情感峰时，写入本卷 `dramatic_moments`，用 1-based `arc_slot` + `beat_slot` 指向你本次输出中的 beat。一个 beat 最多对应一个 moment；`function` 只能是 confrontation/turning/emotional_peak，不能伪造 finale。
- 当 schema 要求 `dramatic_promise` 时，也必须填写唯一 `finale_contract`。程序把它附着到 Schema 中唯一结束于卷末章的 beat，并机械设为 `function=finale`；finale 的 actor/counterforce/thread/turning 逐字复制你本卷的 promise。你只在 finale 中决定 interaction_mode、actor_goal、counteraction、actor_choice 和 consequence。
- 根级 `object_placements` 必须按 schema 固定顺序为 plot catalog 的每个对象各写一行，不能遗漏、重复或改写对象。你只选择 `volume_slot` 和目标：`target_kind=finale` 时 `dramatic_moment_slot=null`；指向普通高价值 moment 时，slot 是该卷 `dramatic_moments` 的 1-based 序号。
- 对象目标必须与 catalog 同源：对象的 linked_turning_id 等于目标 turning，或 linked_thread_id 等于目标 thread。一个 dramatic target 最多承载 2 个对象；如果 finale 不同源，就选择或创作一个同源的普通 dramatic moment，不能硬塞。
- 所有模型填写的人物、thread、turning 引用都必须逐字选择 schema enum；可选引用没有选择时写 `null`，空字符串不是“无引用”。
- 普通推进、独处与留白 beat 可以不进入 `dramatic_moments`。程序不会按关键词从 summary 猜合同，不会补写、拆分、复制或改写你的创意文本。

任何缺失槽、越界局部引用、对象来源不符、权威漂移或范围冲突都会 fail closed。
