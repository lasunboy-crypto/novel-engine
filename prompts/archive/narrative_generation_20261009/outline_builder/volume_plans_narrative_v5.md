# 卷规划创意增量（实验管线 v5 / 卷末槽说明 v4）

你负责每卷标题、概要、戏剧承诺、上游线选择、arc/beat 的叙事功能、剧情推进、must/forbidden、冲突与反作用、人物选择、后果和张力变化。程序只负责冻结事实、必达结构槽、稳定 ID、局部引用、由冻结章数推导的可行数量/区间边界和最终装配；不会替你决定固定桥段、场景、对白或叙事内容。

严格遵守随请求提供的 JSON Schema，只输出 JSON。不要输出程序生成的当前 volume/arc/beat 稳定 ID、chapter_count、tension_template、系统预算、状态或 hash。除 schema 明确给出的 ID/slot 外，不要自行创造引用。

- `volumes` 数量和顺序逐一对应冻结总卷数；每卷 `tension_curve` 长度必须精确等于冻结章数，元素只能是 1–10 整数。程序不会 pad、trim 或 clamp。
- linked 字段只能逐字选择上游已有 ID。单卷没有合法后续卷时，`dramatic_promise.outcome` 只能是 `resolved`，两个 `deferred_to_*` 都必须为 `null`；多卷只允许非末卷选择 schema 给出的后续卷。
- 程序会先从冻结章数和每 arc 最少 beat 数中移除数学上不可能的 arc 数量。当 Schema 用 `prefixItems` / `enum` 冻结 `chapter_range` 时，只能逐字填写给定骨架，不得新增、复制、重叠或改写范围；这只冻结结构坐标，不替你决定剧情。
- Schema 未冻结具体范围时，普通产品每个 arc 仍由你在可行上限内选择 2–4 个 beats；selection_short_arc 路径每个 arc 恰好 1 个 beat。每个 beat 的 summary、must/forbidden 始终由你负责；所有自选范围必须连续、无重叠并覆盖所属 arc。
- `beats` 只写基础创意内容，不再嵌套 `dramatic_contract`。需要额外高价值对抗、转折或情感峰时，写入本卷 `dramatic_moments`，用 1-based `arc_slot` + `beat_slot` 指向你本次输出中的 beat。一个 beat 最多对应一个 moment；`function` 只能是 confrontation/turning/emotional_peak，不能伪造 finale。
- 当 schema 要求 `dramatic_promise` 时，也必须填写唯一 `finale_contract`。程序把它附着到 Schema 中唯一结束于卷末章的 beat，并机械设为 `function=finale`；finale 的 actor/counterforce/thread/turning 逐字复制你本卷的 promise。你只在 finale 中决定 interaction_mode、actor_goal、counteraction、actor_choice 和 consequence。
- 卷末 beat 已由 `finale_contract` 独占，绝不能再用任何 `dramatic_moments` 行指向它；“一个 beat 一个合同”也包含这个自动附着的 finale。若末尾还有转折或情感峰，把其行动与后果设计进 finale，或选择前面尚未占用且叙事成立的 beat。输出前检查所有 `(arc_slot, beat_slot)` 互不重复且都避开卷末 beat。
- 根级 `object_placements` 必须按 schema 固定顺序为 plot catalog 的每个对象各写一行，不能遗漏、重复或改写对象。你只选择 `volume_slot` 和目标：`target_kind=finale` 时 `dramatic_moment_slot=null`；指向普通高价值 moment 时，slot 是该卷 `dramatic_moments` 的 1-based 序号。
- 对象目标必须与 catalog 同源：对象的 linked_turning_id 等于目标 turning，或 linked_thread_id 等于目标 thread。一个 dramatic target 最多承载 2 个对象；如果 finale 不同源，就选择或创作一个同源的普通 dramatic moment，不能硬塞。
- 所有模型填写的人物、thread、turning 引用都必须逐字选择 schema enum；可选引用没有选择时写 `null`，空字符串不是“无引用”。
- 引用可单项为空，不代表整组可为空：每个 `dramatic_moments` 的 `thread_id` 与 `turning_id` 至少选择一个既有来源；`dramatic_promise` 的 `primary_thread_id` 与 `primary_turning_id` 也至少选择一个。可以同时选择两项，但都必须与实际剧情同源；程序不会替你选来源。没有这样的来源时，普通推进可以只写在 beat 中，不要虚构高价值合同。
- 每份 moment 和 promise 的 `actor_id` 与 `counterforce_id` 必须是不同人物；独处、内心挣扎不能把同一人物填入两边。间接对抗可用 `interaction_mode=indirect`，仍须有真实的另一人物及其反作用。
- `dramatic_promise` 与 `finale_contract` 必须成对出现；有任何 `dramatic_moments` 时必须同时给出二者。Schema 要求的承诺、finale 和承重物件不得为了消除引用错误而删除。`outcome=resolved` 时两个延后目标都为空；`explicitly_deferred` 时两个目标都要选择合法的后续卷及既有 thread。
- 普通推进、独处与留白 beat 可以不进入 `dramatic_moments`。程序不会按关键词从 summary 猜合同，不会补写、拆分、复制或改写你的创意文本。

任何缺失槽、越界局部引用、对象来源不符、权威漂移或范围冲突都会 fail closed。

## 完整卷内戏剧设计职责

- 卷承诺必须由上游真实主线与人物目标发展而来，写清本卷独有的对抗、反制和结果，避免每卷复制同一节奏。
- arc 与 beat 的概要表达推进过程及条件变化；必达表达本段必须兑现的结果，禁止表达保留边界，不把同一结果改写成多条互相独立的事件。
- 在分配范围之前判断每个 beat 有多少实际可展开的经历：人物行动、反馈、关系回应和后果如何相接。重要选择要有铺垫和余波，普通推进与留白可简洁，不按必达条数或固定场次数制造密度。
- finale 所需人物选择、反制与代价要在前段形成条件。保持设定与揭示顺序，不能靠最后临时增加能力、物件或隐秘经历解决冲突。


## 先安排经历，再分配章节

按上游主线实际经历的分量组织整卷，再划 arc/beat 范围。比较相邻段落：上一段的结果如何改变下一段的条件，哪些只是同一顾虑、等待或家务的重复。没有新分量的准备可合入重要事件的前因或简略过渡，不先按天数、人物出场顺序平均铺满章节，再逐格填劳作与犹豫。

本次你仍能组织 beat 内容与分配章节；后续长度重排会冻结这些剧情文本，不能替你合并重复事件或添加真正缺少的经历。因此在本次输出里完成取舍，不把“先等消息、再等来人、再想清楚”的松散材料留给扩章或 Writer 加速。

dramatic_promise 选择本卷实际兑现的阶段成果，与上游 turning/thread 同源。成长或冒险故事里，若上游已经包含入门后的经历，不能仅因“出发很动情”便把出发挪成卷末成果。若上游本身只支持离别故事，必须保留其权威范围，不能私造后续；这种材料不足需要返回主线修复。

forbidden 保留真实因果、能力、秘密和揭示顺序。先在本次设计中确定合理的事件位置，再写确实需要的边界，不为了让高潮落在某个槽位，新增一连串“此时不得尝试、不得答应、不得取得任何结果”。已有世界、人物与上游明确承诺不能删除。普通讨论、试探、宽慰和心理不自动等于未来结果兑现。

人物认识与生活来路可随事件交代，日常、修习、休整和感情可有分量；不要求每章对抗或突破。must 只保留必要事件结果，不把心理写法、日常步骤、机位和对白变成验收条款。重要选择的铺垫与余波可以共存，同一核心决定只发生一次，具体详略留给 Writer。
