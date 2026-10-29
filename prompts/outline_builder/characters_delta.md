# 角色创意增量（Hy3 strict schema）

你负责完整的人物设计：姓名、角色定位、原型、外貌、性格、背景、欲望/动机、声音、多重身份、关系张力与正文前承重事实。程序只分配最终稳定 ID，并把局部引用映射回完整 `CharactersOutput`。

严格遵守 Provider JSON Schema，只输出 JSON。不要输出 `character_id`、workspace、时间戳、hash 或 `evidence_quote`。

- `characters` 保持 4–8 人；数组顺序是本次唯一局部 slot（从 1 开始）。
- `relationships[].target_slot` 只能指向同一数组内另一人；关系内容由你创作，允许空数组，禁止自指和重复 target。
- `initial_core_facts` 只选择正文开始前已成立、真正承重的事实；`value` 必须逐字出现在同一角色所选 `source_field`，没有则明确输出空数组。程序只做逐字定位，不会替你补事实。
- 普通角色可以没有 `identity_facets`，不要为结构完整而强塞双重身份。

不得套用题材固定性格、关系或人物模板；任何创意必填字段缺失都会 fail closed。
