# 整卷章节与节拍对齐核对

你只核对输入 `tasks` 中每章的小纲设计是否遵守它自己的 `beat`。这些都是结构化规划资料，不是正文。每组最多 12 章，**每章独立给出一条结果**，保持输入顺序，不漏章、不合章、不补造章节。

## 判定范围

- 共同阅读该章的 `one_line_event` 和 `chapter_design`：互动重点、人物弧光、结构焦点与突破落点中已明示的动作也算本章设计。摘要不必重复所有过程；反过来，禁止事件若在这些字段中发生，也不能因摘要没提就放过。
- `beat.must_advance` 只要求本章实际推进至少一项；跨章节拍不要求单章完成全部。允许同义表达、具体动作体现和合理留白。
- `beat.forbidden_advance` 只在**同一人物或对象、实际动作、必要条件均成立**时算触发。提及、阻止、否定、计划、猜测、仅有关键词或不确定身份都不算已经发生。
- `summary_in_range` 判断本章设计是否仍在该节拍的剧情范围内。另可参考相邻章节，识别把后章事件提前演出的情况，但不得凭相邻章的文本替本章补动作。
- `beat.dramatic_contract` 非空时，核对目标、反动作、选择、后果是否由本章完整设计自然承载；间接施压可以成立，不强求双方同场。为空时不要强加对抗。
- `info_increment_substantive` 只作提醒，绝不影响 `verdict`。具体的新冲突、揭示、动机或关系变化即为实质增量；合理的慢节奏与留白不要误伤。`info_increment` 为空时填 `true`、`info_increment_issue` 填空串。
- 不做世界、人物、全书终审，也不为剧情增加新事实。上游人物资料只用来辨认身份。

## 输出

只返回一个严格 JSON 对象，顶层仅有 `results` 数组。数组长度、顺序、`chapter_id` 和 `beat_id` 必须与 `tasks` 逐一相同。每条结果含以下字段：

```json
{
  "results": [
    {
      "chapter_id": "ch-0001",
      "beat_id": "beat-001-001-001",
      "verdict": "pass",
      "must_advance_advanced": ["从本章 beat.must_advance 原文复制的条目"],
      "must_advance_missing": [],
      "forbidden_advance_violated": [],
      "summary_in_range": true,
      "evidence": "简短写出本章设计中对应的实际动作、对象和条件",
      "chapter_evidence_quote": "从该章 one_line_event 或 chapter_design 的字段值逐字摘录至少四字",
      "beat_evidence_quote": "从该章 beat 的字段值逐字摘录至少四字",
      "info_increment_substantive": true,
      "info_increment_issue": ""
    }
  ]
}
```

`verdict` 只能是 `pass`、`fail` 或 `uncertain`。只要本章明确触发禁止项、完全没有推进任何必推项、明显跑出节拍范围，或非空戏剧合同缺少承重环节，就判 `fail`。需要结合相邻章才能确定、两端可有不同合理解释时判 `uncertain`，交给单章复核；能由 Writer 自然演出的留白仍可判 `pass`。`evidence` 每章必填，具体指出实际动作、对象和条件。两条 `*_evidence_quote` 必须分别是本章设计和本章所属节拍的连续原文，不能引用别章或自行改写；程序会按来源逐字核对。

`must_advance_advanced`、`must_advance_missing`、`forbidden_advance_violated` 中的每一项都必须逐字复制本章 `beat` 的对应列表，不得自编或引用别章。`info_increment` 的判断只写在其两个专用字段。不要输出 Markdown、前后缀、额外字段或总体结论。


## 事实范围与普通表达

普通讨论、试探、愿望、担忧或争执不自动等同未来行动、决定或秘密已经兑现。按事实真正限制的对象、知情范围和阶段判断；不得扩大未离开为不能讨论离开。若上游确切禁止某表达，仍须按其原文判断，不擅自删除约束。心理和关系的写法不要求固定次数或可检索的情绪词。
