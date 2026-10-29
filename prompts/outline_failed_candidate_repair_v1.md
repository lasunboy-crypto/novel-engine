# 来源绑定的失败大纲候选修复 v1

你只修复一个**尚未写入正式大纲**的隔离候选。完整的四段原文、审核逐字引文、
允许修改的字段和修复方向都在输入 JSON 中。请消除本次列出的全部硬矛盾，
保留其它人物、因果、章节范围、ID 和剧情目标。不得添加未经来源支持的绝对规则。

只输出符合 schema 的一个 JSON object：`source_hash`、`changes`、
`contract_changes`、`explanation`。每个获准的字符串字段恰好提交一条
`changes`，其中 `old` 与原文逐字相同，`new` 是整字段的新全文，不是差异片段。
每个获准的 `.dramatic_contract` 对象恰好提交一条 `contract_changes`，其中
`old` 是原对象完整 JSON，`new` 也是同字段结构的完整对象；只允许调整该 Beat
的行动者、对抗、turning/thread 来源、选择和后果，使它与该 Beat 的
summary/must/forbidden 对齐。不可移动或增删 Beat，不可改 arc/beat/chapter ID。

不得提交准入字段之外的改动。若修复方向无法解决全部冲突，就让本次响应不
满足 schema/边界，交由产品拦下；不要偷偷修改其它字段或把同一结果放到两个 Beat。
`explanation` 用短句说明每项矛盾如何消除，不把解释写进故事字段。
