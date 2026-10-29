# 卷规划创意增量（实验管线 v8 / 事实表达分离）

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


## 在冻结前组织主要经历与篇幅

读取scope_experience、scope_outcome、progression_design和主线development。先组织本卷实际要经历的事情及结果用途，再划arc/beat范围。进入条件只是开展主要经历的来由；主线已安排的学习、尝试、运用及后果应有展开空间，不能挤到卷末才刚开始。准备、讨论与等待若仍处同一局面，可合入重要经历或简略过渡。重要的心理、亲情与安静交流按实际分量保留，不按动作密度判断节奏。

阶段space_share是选择空间的参考，允许依真实分量调整，不硬换算章号；张力分数也不等于推进。冻结后重排只能调范围，不能补故事，所以本次就完成重复材料的取舍。opening_segment卷末兑现本段阶段成果并打开下一段；resolved只闭合本卷承诺，长线可以继续。

## 戏剧事实与表达意图区分（合同v2）

dramatic_moments与finale_contract使用contract_version=2并填写required_events。这个数组只保存真正必须发生的事件、决定、承重因果条件与结果，不能漏掉使后果成立的选择和代价。actor_goal、counteraction、actor_choice与consequence完整设计人物眼前利害、阻力和戏剧意图；具体怎么说、怎样停顿、表情、心理和微动作留给Writer。上述四段不再整段升级为验收项；其中真正承重的事实由你明确提炼到required_events。

beat.must_advance与forbidden_advance同样只保存必要结果与真实披露边界。人物愿望、争执、倾诉或心理不自动兑现未来行动。不能为拖延到高潮而新增一串“现在不得尝试／不得答应”的禁令。世界完整规则继续约束实际结果，但第一次接触只安排本章必要观察，未用的条件与极限可以暂不揭示。

物件relation_attributes是全书可追踪属性目录，不要求每次出现时填写全部关系。物理持有、决定授权、可操作权限是不同事实；照料不证明占有，后文取得不能提前发生。登记本章明确的一义即可，不设计归属宣言来满足机器。
