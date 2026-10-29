# 上游事实锁定表修改前模板

2026-09-15。以下逐字保留旧构造函数；动态人物原文仍以各次原始请求为准。

```python
def build_name_lock_header(characters: CharactersOutput) -> str:
    """建可注入的"全书 canonical 事实锁定表" header 块(v2 W3.5 补强1 泛化 + (a) 关系硬锁)。

    锁五类 canonical 事实(同一确定性机制):① 核心角色名(取自角色表,reinforce
    防 canonical 名漂移〔苏畅→苏偿〕)② 关联人物名(buried,无独立 ID)③ 时间锚点
    ④ 地点 ⑤ **角色关系**(v2 W3.5 (a):relationships_brief 不传下游 → 叔伯写成父子 /
    恩师拆两人,强制触发 RCT 12%→75%)。下游各阶段 ``_build_*_user_message`` 拼 prompt
    时调本函数前置注入。

    **触发条件**:有 buried 名 / 时间锚点 / 地点 / **角色关系**任一非空才输出(纯角色名
    已在 character_lines、无 backstory 事实 + 无关系的简单 premise 不必扰动)。**含任一时
    一并列出全部核心角色名**——苏畅→苏偿实证 canonical 名虽在 character_lines 仍会漂,
    显式锁定加固。**注**:绝大多数真实大纲有 relationships_brief → header 常态输出关系锁
    (这正是 (a) 治本目标:每本书都锁住关系防漂移);丰富度对调盲读已验不压扁 deepseek。

    :return: 事实锁定 header 字符串(含尾随空行);无任何可锁事实时返回 ``""``。
    """
    buried = extract_locked_names(characters)
    times = extract_time_anchors(characters)
    places = extract_place_anchors(characters)
    relationships = build_relationship_lock_lines(characters)
    if not buried and not times and not places and not relationships:
        return ""

    canonical = [c.name for c in characters.characters if c.name]
    lines = [
        "## ⚠️ 全书 canonical 事实锁定表(必须逐字沿用,严禁改写 / 换字 / 改数字 / 另起别名)",
        "",
        "下列人名与关键事实已在上游角色档案中确定,是全书**固定设定**。本阶段及后续所有",
        "产出中凡涉及,**必须逐字使用**,严禁改写、换字、为同一对象生造别名、或改动数字 ——",
        "即使某人物没有独立 character_id,其名字同样不可改动。",
        "",
        "**【核心角色名】** " + "、".join(canonical),
    ]
    if buried:
        lines.append("**【关联人物**(无独立 ID,尤需锁定)**】**")
        for ln in buried:
            lines.append(
                f"- {ln.name} —— 来源:「{ln.snippet}」" if ln.snippet else f"- {ln.name}"
            )
    if times:
        # A(补强1 收尾追气密):时间锁不气密残留 = 数字被反复改写时滑成别的年数(ON rep4
        # 同事件 20/30 矛盾)。故把时间从"干列数字"强化为"绑定其指代事件 + 点名禁止漂移
        # 模式(同一事件不可一处 X 一处 Y)",把锚定打到事件粒度。
        lines.append(
            "**【关键时间锚点**(下列时间数字全书固定;凡指代各自锚定的事件,一律逐字用该"
            "确切年数,严禁改写成别的数字 —— 同一事件不可一处「二十年前」、另一处「三十年前」)**】**"
        )
        for ln in times:
            lines.append(
                f"- **{ln.name}** —— 锚定事件:「{ln.snippet}」"
                if ln.snippet
                else f"- **{ln.name}**"
            )
    if places:
        lines.append("**【关键地点**(地名不可改)**】** " + "、".join(ln.name for ln in places))
    if relationships:
        # (a) 关系硬锁:钉身份(谁是谁的叔伯/恩师/师徒/阵营固定),放开剧情(关系如何发展/对抗自由)。
        # 强制触发 RCT:不传=12% 写对、软列=50%、本硬锁=75%;丰富度对调盲读 deepseek 仍 4/4 胜 qwen 不压扁。
        lines.append(
            "**【角色关系锁定**(下列角色间的**身份 / 血缘 / 师承 / 阵营关系**是全书固定设定,"
            "**严禁改写关系本身** —— 叔伯就是叔伯,不可写成父子 / 祖孙 / 兄弟;恩师就是恩师,"
            "不可拆成两人或错认他人。关系**如何发展、对抗、转变**由你自由发挥,但**关系的身份"
            "定义不可变**)**】**"
        )
        lines.extend(relationships)
    lines.append("")
    return "\n".join(lines)
```
