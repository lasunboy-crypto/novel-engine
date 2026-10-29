"""大纲生成端共享:全书 canonical 事实锁定表(deterministic fact lock,v2 W3.5 补强1)。

为什么需要(根因 docs/findings/v2_w3_5_deepseek_governability_eval_results.md §5b/§9)
============================================================================
deepseek = "语义优先"模型:强语义创作、弱符号边界。在大纲 plot 生成阶段,它
**不 honor 埋在角色档案自由文本里的 canonical 事实**。s7 实证:主角顾云之父在
characters 档案叫"顾崇山"(只在 ``background`` 字段提及、**无独立 character_id**),
plot 阶段 deepseek 另起"顾盛礼"×13 并向下游 volume_plans 传染。

锁定范围(v2 W3.5 补强1 泛化,Feng 2026-06-25 拍板)
====================================================
实证(`scripts/v2_w3_5_namelock_validate.py` 10×OFF 重产 + fresh 读)证明漂移
**不止命名,是整类"符号边界"弱**:同篇里时间(二十年前→二十五年前)、连有独立
ID 的 canonical 角色名(苏畅→苏偿)都会漂,各 ~1/10。**架构追链证 outline 漂移
逐层放大吞噬正确事实**(run0:顾盛礼在 plot 5:1 压过顾崇山 → volume_plans 0:5
正确名彻底消失)→ 下游每章继承错名 → 撞车 / must 不可达成 / 时间线矛盾。outline
是每本一次性消费 → 源头准确预防值得且必须。

故本模块从"命名锁定"泛化为 **canonical 事实锁定**,同一确定性机制锁:
- **① 核心角色名**(顾云/苏畅…):直接取自角色表,reinforce 防 canonical 名漂移;
- **② 关联人物名**(顾崇山):锚点+姓氏挖 buried 名(无独立 ID 者);
- **③ 时间锚点**(二十年前/日期):正则挖,**时间下游无 §2.50 检测、防御最薄、优先**;
- **④ 地点**(西域/罗布泊):区域 seed + 地名后缀词 + 锚定专名;
- **⑤ 角色关系**(黎苍是黎朔叔伯/魏渊是陆沉恩师):**v2 W3.5 (a)(2026-06-26)**——
  ``build_relationship_lock_lines`` 把 relationships_brief 按对去重渲染成关系硬锁。
  根因同 ②:relationships_brief 不传下游 → deepseek 写主线/卷规划时拿不到关系身份事实,
  自信填空(叔伯→父子、恩师拆两人)。强制触发 RCT 实证:不传 12%/软列 50%/硬锁 75%;
  丰富度对调盲读 deepseek+硬锁 4/4 胜 qwen 不压扁。**钉身份(关系定义固定)、放开剧情
  (关系如何发展自由)**。

物理根因 = **上游软交接**:``_build_plot_user_message`` 只传 ``character_id /
name / role / archetype / motivation``,**不传 background / relationships_brief``
→ 这些 canonical 事实埋在 plot 看不见的字段里,被当"软提示"随意改写。

物理根因 = **上游软交接**:``_build_plot_user_message`` 只传
``character_id / name / role / archetype / motivation`` 5 个字段,**不传
background / relationships_brief** —— 父亲名埋在 plot 看不见的字段里,于是被
当成"软提示"随意改写。

治理之道(Feng 2026-06-24 拍板硬约束:只上游修复 + 不堆 agent + 确定性规则)
============================================================================
**确定性**(零 LLM、无新 agent)扫角色自由文本,把"无独立 ID 但已被设定的人名"
抽成全书 canonical 命名锁定表,注入下游各阶段 prompt header 显式声明"不可改写"。
把命名从"埋在 prose 的软提示"升级成"显式硬约束"(钉 WHAT、不损 HOW)。

提取信号(validate-first,4 个真实大纲样本测得精度=召回=100%)
==============================================================
纯"姓氏字开头 token"计数噪声极大(成为/关系/任何/文物… 全是常用词假阳性)。
**高精度信号 = 亲属/称谓锚点 + 紧跟百家姓**:
- 锚点("其父"/"父亲"/"师父"/"名叫"…)后必须紧跟**百家姓**才算命中 ——
  "父亲留下的笔记"里"父亲"后跟"留"(非姓氏)直接排除,这是高精度的来源。
- 边界消歧(顾崇 vs 顾崇山):取在语料里复现 ≥2 的更长者。
- 排除 canonical 角色名及其子/超串(周知远→周知、陆沉→陆沉的)。

**已知 caveat(推理,非实证)**:锚点法对"无亲属/称谓引导的裸名"(纯地名/
组织名/路人裸名)召回为 0。这是精度优先的有意取舍 —— 本案要治的"关联角色名"
(父/师类)恰好都有锚点,且注入垃圾污染 prompt 的代价高于漏抓低风险裸名。
真正的端到端裁决 = 注入后 deepseek 重产大纲是否还造别名(真 LLM 验证)。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
import re

from src.core.models.outline import CharactersOutput

# ---------------------------------------------------------------------------
# 百家姓(常见单字姓 top ~120,覆盖绝大多数中文姓名)
# ---------------------------------------------------------------------------
_SURNAMES: frozenset[str] = frozenset(
    "王李张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹"
    "袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚"
    "谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤尹黎易常武乔"
    "贺赖龚文庞樊兰殷施陶洪翟安颜倪严牛温芦季俞章鲁葛伍韦申尤毕聂焦向"
    "柳邢路岳齐梅莫庄辛管祝左涂谷祁时舒耿牟卜詹关苗凌费纪靳盛童欧甄"
    "项曲成游阳裴席卫查屈鲍覃霍翁隋甘景单包司柏宁柯阮桂闵解强"
)
# 复姓
_COMPOUND_SURNAMES: tuple[str, ...] = (
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "夏侯",
    "诸葛", "尉迟", "皇甫", "公孙", "慕容", "长孙", "宇文", "司徒", "司空",
    "令狐", "百里", "呼延", "西门", "第五", "拓跋", "轩辕", "钟离",
)
# 高精度名字引导锚点(锚点后紧跟姓氏才算命中)。长锚点在前,保正则 leftmost 优先匹配。
_ANCHORS: tuple[str, ...] = tuple(
    sorted(
        (
            "外祖父", "外祖母", "其父", "其母", "亡父", "亡母", "先父", "先母",
            "父亲", "母亲", "祖父", "祖母", "师父", "师傅", "师尊", "恩师",
            "兄长", "弟弟", "妹妹", "姐姐", "儿子", "女儿", "丈夫", "妻子",
            "夫人", "叔叔", "伯父", "舅舅", "名叫", "叫做", "称为", "名为",
            "本名", "原名", "代号", "父", "母",
        ),
        key=len,
        reverse=True,
    )
)

_HANZI = "[一-鿿]"
# 末尾常见虚词/代词(边界过切后修剪)
_PARTICLES: frozenset[str] = frozenset("的了是在和与及也都很就还又这那他她它你我之其将曾")

_ANCHOR_ALT = "|".join(re.escape(a) for a in _ANCHORS)
_COMPOUND_ALT = "|".join(_COMPOUND_SURNAMES)
# 锚点 + 可选"的" + (复姓+1~2字)  /  (单字姓+1~2字)
_ANCHORED_COMPOUND_RE = re.compile(
    rf"(?:{_ANCHOR_ALT})的?((?:{_COMPOUND_ALT}){_HANZI}{{1,2}})"
)
_ANCHORED_SINGLE_RE = re.compile(rf"(?:{_ANCHOR_ALT})的?({_HANZI}{{2,3}})")
_SENTENCE_SPLIT_RE = re.compile(r"[。!?；\n]")

# 时间锚点:中文/阿拉伯数字 + 年(前)。挖背景里的"二十年前/两千年"等事件时间戳。
_TIME_RE = re.compile(r"(?:[零一二三四五六七八九十百千万两]{1,6}|\d{1,4})年前?")
# 距离单位(用于过滤零碎"一年/两年"——只锁含整十百千万的距离或显式"年前"事件锚)
_DISTINCTIVE_UNITS = frozenset("十百千万")

# 地点(开放式专名,确定性抽取最难——每个地理后缀都撞常用词〔域↔领域/区域、漠↔
# 冷漠、山↔人名崇山〕。故走三条干净信号,validate-first 在 run0 测得零撞名:
#   ① 区域/地貌 seed(精确匹配,闭集零噪声)
#   ② 本身即地名的后缀词(绿洲/遗址/古城…,不撞常用词)
#   ③ 位置锚点 + 专名(在罗布泊;泊 单独会撞"停泊/漂泊",必须锚定)
# **诚实 caveat**:无清晰后缀的虚构/历史地名(楼兰/敦煌类)会漏——确定性抽不出
# 开放专名(需 gazetteer 或 LLM,均违硬约束);但地点 premise/world 钉得死、实测稳
# (10×OFF 全在西北大漠域内无矛盾),区域级锁定已覆盖主要漂移面。
_REGION_SEEDS: tuple[str, ...] = (
    "西北", "西南", "东北", "东南", "华北", "华南", "江南", "中原", "塞外", "塞北",
    "漠北", "漠南", "河西", "西域", "关中", "岭南", "大漠", "戈壁", "荒漠", "沙漠",
    "雪原", "草原", "高原", "盆地", "绿洲", "雪域", "北疆", "南疆", "西陲", "边塞",
)
# 本身即地名的后缀词(低撞词):前缀最多 2 字(防 boundary bleed)
_PLACE_WORD_RE = re.compile(
    r"([一-鿿]{0,2}(?:绿洲|遗址|古城|废墟|秘境|雪山|火山|冰川|草甸|盐湖|峡谷))"
)
# 位置锚点 + 以"泊"结尾的专名(罗布泊;泊 易撞停泊/漂泊,故必锚定)
_PLACE_ANCHORED_RE = re.compile(
    r"(?:在|于|位于|失踪[在于]|出土[在于]|深入|前往|抵达|来自|远赴)([一-鿿]{2,4}?泊)"
)


class LockedName:
    """一个被锁定的全书 canonical 命名。

    :param name: 命名原文(逐字锁定)。
    :param occurrences: 在角色档案语料中的出现次数。
    :param snippet: 首次出现所在句子(供下游 prompt grounding "这是谁")。
    """

    __slots__ = ("name", "occurrences", "snippet")

    def __init__(self, name: str, occurrences: int, snippet: str) -> None:
        self.name = name
        self.occurrences = occurrences
        self.snippet = snippet

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"LockedName(name={self.name!r}, occurrences={self.occurrences})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LockedName):
            return NotImplemented
        return (self.name, self.occurrences, self.snippet) == (
            other.name,
            other.occurrences,
            other.snippet,
        )


@dataclass(frozen=True)
class RelationshipLockCharacter:
    """关系硬锁渲染所需的最小角色视图。

    Outline 阶段使用 ``CharacterOutlineItem.relationships_brief``;writer 阶段从
    SQLite ``Character.relationships`` 读同源关系事实。两者字段名不同,但硬锁渲染
    只需要 ``character_id/name/relationships_brief`` 三项,所以抽成最小视图避免
    writer 为复用渲染逻辑构造完整 Outline Pydantic 模型。
    """

    character_id: str
    name: str
    relationships_brief: dict[str, str]


def _build_corpus(characters: CharactersOutput) -> str:
    """拼接所有角色的自由文本字段(buried 名可能出现的地方)。"""
    parts: list[str] = []
    for c in characters.characters:
        for field in (c.appearance, c.personality, c.background, c.motivation):
            if field:
                parts.append(field)
        for rel_text in c.relationships_brief.values():
            if rel_text:
                parts.append(rel_text)
    return "\n".join(parts)


def _trim_particle(name: str) -> str:
    """修剪边界过切带入的末尾虚词/代词(顾崇山是 → 顾崇山)。"""
    while len(name) > 2 and name[-1] in _PARTICLES:
        name = name[:-1]
    return name


def _resolve_boundary(raw: str, corpus: str, *, is_compound: bool) -> str:
    """边界消歧:复现 ≥2 的更长者优先,否则更短者,单次出现默认更长(中文名多 2~3 字)。"""
    n_long = 4 if is_compound else 3
    n_short = 3 if is_compound else 2
    long_name = _trim_particle(raw[:n_long])
    short_name = _trim_particle(raw[:n_short])
    if len(long_name) >= n_short and corpus.count(long_name) >= 2:
        return long_name
    if corpus.count(short_name) >= 2:
        return short_name
    return long_name


def _sentence_of(corpus: str, name: str) -> str:
    """name 首次出现所在的句子(去首尾空白;供 grounding)。"""
    idx = corpus.find(name)
    if idx < 0:
        return ""
    for sentence in _SENTENCE_SPLIT_RE.split(corpus):
        if name in sentence:
            return sentence.strip()
    return corpus[max(0, idx - 20) : idx + len(name) + 20].strip()


def extract_locked_names(characters: CharactersOutput) -> list[LockedName]:
    """确定性扫角色自由文本,抽"无独立 character_id 但已被设定的人名"。

    精度优先(亲属/称谓锚点 + 紧跟百家姓);排除 canonical 角色名及子/超串。
    返回按出现次数降序的 ``LockedName`` 列表(空 = 无 buried 名)。
    """
    corpus = _build_corpus(characters)
    canonical = {c.name for c in characters.characters if c.name}

    found: dict[str, LockedName] = {}

    def _consider(raw: str, *, is_compound: bool) -> None:
        name = _resolve_boundary(raw, corpus, is_compound=is_compound)
        min_len = 2  # 至少姓 + 1 字
        if len(name) < min_len:
            return
        if name in canonical:
            return
        # 排除 canonical 角色名的子串/超串(周知远↔周知、陆沉↔陆沉的)
        if any((name in cn) or (cn in name) for cn in canonical):
            return
        if name in found:
            return
        found[name] = LockedName(
            name=name,
            occurrences=corpus.count(name),
            snippet=_sentence_of(corpus, name),
        )

    for m in _ANCHORED_COMPOUND_RE.finditer(corpus):
        _consider(m.group(1), is_compound=True)
    for m in _ANCHORED_SINGLE_RE.finditer(corpus):
        token = m.group(1)
        if token[0] in _SURNAMES:
            _consider(token, is_compound=False)

    return sorted(found.values(), key=lambda ln: ln.occurrences, reverse=True)


def extract_time_anchors(characters: CharactersOutput) -> list[LockedName]:
    """确定性抽时间锚点(事件时间戳:二十年前/两千年)。

    只锁**显式"X年前"事件锚** 或 **含整十/百/千/万的距离**(过滤零碎"一年/两年");
    复用 ``LockedName``(name=时间表达式)。返回按出现次数降序。
    """
    corpus = _build_corpus(characters)
    found: dict[str, LockedName] = {}
    for m in _TIME_RE.finditer(corpus):
        expr = m.group(0)
        # 显式"前"=事件锚(三年前/二十年前);否则需含整十百千万(二十年/两千年)
        if not expr.endswith("前") and not any(u in expr for u in _DISTINCTIVE_UNITS):
            continue
        if expr in found:
            continue
        found[expr] = LockedName(
            name=expr,
            occurrences=corpus.count(expr),
            snippet=_sentence_of(corpus, expr),
        )
    return sorted(found.values(), key=lambda ln: ln.occurrences, reverse=True)


def extract_place_anchors(characters: CharactersOutput) -> list[LockedName]:
    """确定性抽地点(区域 seed + 本身即地名后缀词 + 锚定专名)。

    精度优先(见 ``_REGION_SEEDS`` 段 caveat:开放专名漏抓是有意取舍,over-lock 真
    地点无害)。复用 ``LockedName``(name=地点)。返回按出现次数降序。
    """
    corpus = _build_corpus(characters)
    found: dict[str, LockedName] = {}

    def _add(place: str) -> None:
        place = place.strip()
        if len(place) < 2 or place in found:
            return
        found[place] = LockedName(
            name=place,
            occurrences=corpus.count(place),
            snippet=_sentence_of(corpus, place),
        )

    for region in _REGION_SEEDS:
        if region in corpus:
            _add(region)
    for m in _PLACE_WORD_RE.finditer(corpus):
        _add(m.group(1))
    for m in _PLACE_ANCHORED_RE.finditer(corpus):
        _add(m.group(1))
    return sorted(found.values(), key=lambda ln: ln.occurrences, reverse=True)


def build_relationship_lock_lines(characters: CharactersOutput) -> list[str]:
    """渲染"角色关系锁定"行(v2 W3.5 (a) 关系硬锁,2026-06-26)。

    根因(强制触发 RCT 验证,12%→75%):``_build_plot_user_message`` 等只递
    ``id/name/role/archetype/motivation`` 给下游出大纲阶段,**不传 relationships_brief**
    —— "黎苍是黎朔的叔伯""魏渊是陆沉的恩师"这类**关系身份事实**埋在 plot 看不见的字段
    里 → deepseek 写主线 / 卷规划时拿不到,自信地填空(叔伯写成父子 / 把恩师拆成两人)。
    这是 namelock 同款软交接,只是当初只捞了名字、漏了关系。

    机制 = 同 namelock:确定性把 relationships_brief 抽成全书关系锁定行,注入下游 prompt,
    把关系从"埋在 prose 的软提示"升级成"显式硬锁"。**按无序角色对去重**(A↔B 只列一次,
    取先遇到的一向,身份信息已足以锁定)。空 relationships_brief → 返空 list。

    :return: 关系锁定行 list(不含 header 标题行);无关系时返回 ``[]``。
    """
    return build_relationship_lock_lines_from_records(
        RelationshipLockCharacter(
            character_id=c.character_id,
            name=c.name,
            relationships_brief=dict(c.relationships_brief),
        )
        for c in characters.characters
    )


def build_relationship_lock_lines_from_records(
    records: Iterable[RelationshipLockCharacter],
) -> list[str]:
    """按无序角色对渲染关系硬锁行,供 outline 与 writer 共享。

    ``records`` 可以只包含本章相关角色 + 被关系指向的目标角色。只有有
    ``relationships_brief`` 的记录会产出关系行;目标角色若仅用于 ID→姓名解析,
    ``relationships_brief`` 可为空。
    """
    items = list(records)
    id_to_name = {c.character_id: c.name for c in items}
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    for c in items:
        for other_id, rel in c.relationships_brief.items():
            rel_text = (rel or "").strip()
            if not rel_text:
                continue
            pair_key = tuple(sorted((c.character_id, other_id)))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            other_name = id_to_name.get(other_id, other_id)
            lines.append(f"- {c.name} 与 {other_name}:{rel_text}")
    return lines


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
        "## ⚠️ 全书 canonical 事实锁定表(专名与既往事实;关系发展参考另列)",
        "",
        "下列人名、地名及已发生事件的时间已在上游角色档案中确定。本阶段及后续所有",
        "产出中凡引用这些专名或既往事实,**必须沿用原文**,严禁换字、为同一对象生造别名、或改动数字 ——",
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
        # 关系原文含身份、前史和压力，锁定范围不能覆盖整段自由文本。
        # 强制触发 RCT:不传=12% 写对、软列=50%、本硬锁=75%;丰富度对调盲读 deepseek 仍 4/4 胜 qwen 不压扁。
        lines.append(
            "**【角色关系锁定(仅既定身份与前史)及发展参考】**\n"
            "以下为关系原文,其中已确立的身份、血缘、师承及共同前史必须保持,"
            "**严禁改写关系本身的既有事实** —— 叔伯就是叔伯,不可写成父子 / 祖孙 / 兄弟;"
            "恩师就是恩师,不可拆成两人或错认他人。当前压力、愿望和发展设想仍是创作参考,"
            "不代表后续行动已经完成,也不增加本章必达事件。关系**如何发展、对抗、转变**"
            "由你自由发挥,随主线及本章行动推进;既往身份不能被改写,关系原文不要求逐字复述。"
        )
        lines.extend(relationships)
    lines.append("")
    return "\n".join(lines)
