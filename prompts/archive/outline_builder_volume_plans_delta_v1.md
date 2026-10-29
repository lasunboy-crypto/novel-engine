# 卷规划创意增量（Hy3 strict schema）

你负责每卷标题、概要、戏剧承诺、上游线选择、arc/beat 内容、must/forbidden、冲突与反作用、章区间和张力变化。程序从 New Book 精确注入 `volume_id`、`chapter_count`、`tension_template`，并为 arc/beat 生成稳定 ID，再组装现有 `VolumePlansOutput`。

严格遵守 Provider JSON Schema，只输出 JSON。不要输出 volume/arc/beat 稳定 ID、chapter_count、tension_template、系统预算、状态或 hash。

- `volumes` 数量和顺序逐一对应冻结总卷数；每卷 `tension_curve` 长度必须精确等于冻结章数，元素只能是 1–10 整数。程序不会 pad、trim 或 clamp。
- linked 字段只能选择上游已有 ID；dramatic contract 的人物、伏笔、转折和承重物也只能引用权威目录。
- arc/beat 范围必须连续、无重叠并覆盖全卷；戏剧节拍落点和张力由你决定。
- 空 dramatic contract 是普通推进/留白的合法选择，不按配额强塞高潮。

程序不会编排剧情、分配固定桥段或制造 must/forbidden；任何缺失、冲突或来源漂移都会 fail closed。
