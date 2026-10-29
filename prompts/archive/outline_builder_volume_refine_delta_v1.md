# 单卷长度感知重排增量（Hy3 strict schema）

输入卷的标题、概要、上游引用、arc/beat 身份与全部剧情文本已经冻结。你只在确有戏剧节拍重排需要时，返回新的 `chapter_count`、`tension_curve`、每个既有 arc 的范围和每个既有 beat 的范围。程序把这些变化机械应用到原卷，其余字段逐字复制。

严格遵守 Provider JSON Schema，只输出 JSON。不要回显 volume/arc/beat ID、标题、概要、must/forbidden、dramatic contract、上游引用或模板名。

- `arcs` 与每个 `beats` 的数组顺序、数量必须与输入完全一致，禁止增删或换位。
- 范围必须连续、无重叠并覆盖新章数；张力长度必须精确等于新章数，元素只能是 1–10 整数。
- 不需要重排时也必须返回与原范围完全相同的显式 no-op delta；程序不会替你改戏剧节奏。

缺项、错序、数量变化或来源漂移都会 fail closed，不会回退到完整对象重写。
