# 当前收束与保留事件的同次核对

本次 must_achieve 检查列表可能在原卡片列表末尾附加一个 next_chapter_seed。该末项的索引和来源在用户消息中明示；它是卡片当前收束方向，不会被写回 must_achieve，也不新增正文 HARD。

按同一必要路径判据核对这个 seed 与 forbidden：已经确认的结论与禁止确认同一结论冲突；尚待核对的可见线索与保留后续核对可以共存。不要替 Writer 把“已经发现”改成“只留下线索”后再判通过。仅凭可能存在的越界写法不得报 ambiguous。

沿用原 JSON 字段。发现涉及 seed 时，must_index 必须指向明示末项，must_text 必须逐字复制其输入原文；forbidden_index/text 同样精确回指。普通 must 的索引和语义保持原样。
