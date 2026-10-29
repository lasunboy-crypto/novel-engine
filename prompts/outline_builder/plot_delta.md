# 主线创意增量（Hy3 strict schema）

你负责根本冲突、阶段、转折、长伏笔、人物弧光与承重物的完整因果创意。程序只按数组顺序生成 phase/turn/thread/object 稳定 ID，并把局部 slot 与已知角色 ID 映射成现有 `PlotOutput`。

严格遵守 Provider JSON Schema，只输出 JSON。不要输出任何最终 phase/turn/thread/object ID、workspace、状态、时间戳或 hash。

- 数量和创意密度遵守 schema；数组顺序即局部 slot（从 1 开始）。
- `character_arcs` 使用上游角色的真实稳定 ID；每个角色最多一项。
- 承重物至少选择一个 `linked_turning_slot` 或 `linked_thread_slot`；slot 只指向本次对应数组。
- 人物选择、代价、冲突、揭示、伏笔和物件因果都由你决定；程序不会补反转、桥段或高潮。

不得套固定三幕、题材桥段或每卷同构模板；缺字段、未知引用和歧义引用都会 fail closed。
