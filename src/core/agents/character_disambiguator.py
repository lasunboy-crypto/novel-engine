"""character 语义消歧 LLM-as-judge agent(v1.1 wave 2 session 5 A3 续,
2026-05-29 引入)。

职责:把 ``memory_extractor`` 抽出的本章 ``ExtractedCharacter`` 列表与工作区
已有 ``Character`` 表中的角色做语义消歧判定 — **1 章 1 次综合 LLM 调用**
(沿用 ``plot_thread_merger`` / LLM-as-judge 4 兄弟同型架构),识别本章新抽角色
与既有角色是**同一人不同表述**(描述性指代 vs 真名披露 / 简称别名 / 同人多名等),
返回合并决策让 caller(``memory_extractor._persist_characters``)走"改 character_id
走 update 路径"而非新建独立行。

为什么需要(P7-B-05 规则版兜底,LLM 兜底补漏):

P7-B-05 已有 ``_find_similar_character`` (``difflib.SequenceMatcher`` ratio ≥ 0.85)
按 name 字面相似度做规则版去重 — 拦截"林衍 / 林衍-2 / 林衍二"等 LLM 改后缀
漂移类场景。但**描述性指代 → 真名披露**类场景规则版必然漏抓(典型:
luo-clean-v1 ch-1~10 数据库已有"hui-pao-seng-ren"(灰袍人)/ "pang-guan-yuan"
(胖官员)/ "geng-fu"(更夫)3 个描述性指代角色,后续章节若 LLM 抽出他们的
真名"林衍 / 张大人 / 老王",字面相似度 ≈ 0 → 不命中规则版 → 新建独立行,
实际是同一个人 → writer 检索记忆混乱)。

接口签名:::

    disambiguate_characters_with_existing(
        *, extracted, existing,
        chapter_id, workspace_id=None,
        router=None, client=None, model=None,
        ...
    ) -> CharacterDisambiguationResult

调用方(``memory_extractor._persist_characters`` 入口前置,P7-B-05 规则版
``_find_similar_character`` 之**前**):本章抽取完拿到 ``extracted:
list[ExtractedCharacter]`` 后,先调本 agent 拿合并决策 → 对命中合并的
extracted item,改 ``character_id = target_existing_id`` 让 caller 自然走
get_character hit + update 路径(覆盖字段)。LLM 没命中的 item 继续走 P7-B-05
规则版 0.85 相似度路径作第二道防御。

设计要点
========

1. **1 章 1 次综合 LLM 调用**(沿用 plot_thread_merger / LLM-as-judge 4 兄弟同型):
   - **1st 质量收益**:LLM 一次性看完本章 N 条 extracted + 工作区 M 条
     existing(top-K 候选),综合判定每条 extracted 是否与某条 existing 是
     同一人(包括描述性指代真名披露 / 简称别名 / 同人多名等所有场景)
   - **3rd 工程便利**:每章 +1 次 LLM 调用,成本 ~$0.001-0.005,几乎不计

2. **沿用 cloud_qwen_max_thinking profile + 加入 LLM_AS_JUDGE_AGENTS 集合**:
   合并判定准确度依赖 thinking on 深度斟酌(典型场景"灰袍人 = 张大人"需 LLM
   综合正文上下文 + 角色 description 多维度判断),thinking off 漂移代价高。

3. **existing 双轨筛选**(防 token 爆炸 + 兜底描述性指代):
   - **轨 1**(字面相似度):name SequenceMatcher ratio ≥ 0.3 的 existing 入选
   - **轨 2**(描述性指代兜底,LLM 主战场):existing 中 name 末位含
     ``_DESCRIPTIVE_REFERENCE_SUFFIXES`` 字面("人/官/师/夫/兵/娘/公/婆"等)
     无脑入选 — 真名披露场景字面相似度 ≈ 0 会被轨 1 漏抓,描述性指代
     角色是 LLM 兜底主战场

4. **空输入跳过**:``extracted`` 为空 或 ``existing`` 为空 → 跳过 LLM 调用
   直接返 ``DisambiguationResult(skipped=True, disambiguation_decisions=[])``。

5. **LLM 失败 fail-open**(对齐 LLM-as-judge 同型):LLM 抛异常 / 非法 JSON /
   schema 不符 → 整个 disambiguator 返 ``fail_open=True`` +
   ``disambiguation_decisions=[]``。**fail-open 语义 = 不合并,保守路径** —
   让 P7-B-05 规则版 ``_find_similar_character`` 继续走 0.85 相似度路径作
   第二道防御。

6. **agent_role marker = ``character_disambiguator``**:user message 末尾追加
   ``<!-- agent_role: character_disambiguator -->``,marker 不携带 chapter_id —
   MockLLMClient 识别后默认返 ``decisions: []`` no-disambiguation JSON 让 mock
   路径不影响既有 persist 行为。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.models.character import Character

if TYPE_CHECKING:
    from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_DISAMBIGUATOR_PROMPT_FILENAME = "character_disambiguator_v1.md"

# 默认生成参数:综合判定对采样稳定性敏感,温度低 + token 上限适中(JSON 含
# decisions list + evidence,约 300-1500 token,依赖 extracted/existing 数量)。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1500

# existing 筛 top-K 候选的默认值(防 token 爆炸)。
_DEFAULT_TOP_K_EXISTING = 10

# 描述性指代角色的占位词后缀关键字 — name 末位含这些字面时无脑入选 top-K 候选
# (典型:"灰袍人 / 胖官员 / 更夫 / 老兵 / 老板娘 / 道士 / 和尚"等)。
# 真名披露场景规则版字面相似度 ≈ 0,字面筛选漏掉,这是 LLM 兜底主战场。
_DESCRIPTIVE_REFERENCE_SUFFIXES = (
    "人",
    "官",
    "员",
    "师",
    "夫",
    "兵",
    "娘",
    "公",
    "婆",
    "士",
    "尚",
    "者",
    "客",
    "工",
    "卒",
    "民",
    "翁",
    "汉",
    "妇",
    "童",
)

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹;沿用 LLM-as-judge 同型剥离。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

# trailing HTML 注释剥离。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class CharacterDisambiguationDecision:
    """单条合并决策。"""

    extracted_character_id: str
    target_existing_character_id: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class CharacterDisambiguationResult:
    """disambiguator 输出。

    - ``disambiguation_decisions``:LLM 判定的合并决策 list;无合并时 ``[]``。
    - ``evidence``:LLM 给出的简短判定依据(中文)。
    - ``llm_calls_count``:实际发起的 LLM 调用次数;空字段跳过 → 0;正常 → 1;
      fail-open(LLM 抛异常)→ 1。
    - ``skipped``:为 True 当且仅当 extracted / existing 任一为空走"跳过"分支。
    - ``fail_open``:为 True 当且仅当 LLM 调用抛异常导致整体 fail-open。
    """

    disambiguation_decisions: list[CharacterDisambiguationDecision] = field(
        default_factory=list
    )
    evidence: str = ""
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False


class _DecisionPayload(BaseModel):
    """LLM 输出中单条 decision 字段。"""

    extracted_character_id: str = Field(..., min_length=1)
    target_existing_character_id: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: str = Field(..., min_length=1, max_length=200)


class _DisambiguatorLLMPayload(BaseModel):
    """LLM 输出的中间模型(校验 3 字段)。"""

    verdict: Literal["disambiguate", "no_disambiguate"]
    decisions: list[_DecisionPayload] = Field(default_factory=list)
    evidence: str = Field(..., min_length=1, max_length=400)


def disambiguate_characters_with_existing(
    *,
    extracted: list[dict],
    existing: list[Character],
    chapter_id: str,
    chapter_content_snippet: str = "",
    workspace_id: str | None = None,
    router: "ModelRouter | None" = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> CharacterDisambiguationResult:
    """1 章 1 次综合 LLM 调用,判定本章 extracted 是否与 existing 是同一人。

    :param extracted: 本章 LLM 抽出的 character 候选 list[dict],每条须含
        ``character_id`` / ``name`` / ``description`` 三个 key(``description``
        可以是 archetype + appearance + personality 拼接)
    :param existing: 工作区已有 ``Character`` 实体 list(由 caller 从 SQLite
        读出 + top-K 候选筛选后传入)
    :param chapter_id: 章节 ID,落 structlog 字段
    :param chapter_content_snippet: 章节正文片段(可选,辅助 LLM 判断真名披露
        场景;为空时 LLM 只看角色 description 字段判断)
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("character_disambiguator")`` 取 client + model + 生成参数
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传
    :param model: legacy 路径模型名;``router=None`` 时必传
    :param max_tokens: legacy 路径 token 上限
    :param temperature: legacy 路径温度
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``
    :return: ``CharacterDisambiguationResult``
    """
    if not extracted or not existing:
        _log.debug(
            "character_disambiguator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason="empty_extracted_or_existing",
            extracted_count=len(extracted),
            existing_count=len(existing),
        )
        return CharacterDisambiguationResult(
            llm_calls_count=0,
            skipped=True,
        )

    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="character_disambiguator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(
            template_root / _DISAMBIGUATOR_PROMPT_FILENAME
        )
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_DisambiguatorLLMPayload,
            schema_name="character_disambiguator",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        _log.warning(
            "character_disambiguator_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return CharacterDisambiguationResult(
            llm_calls_count=0,
            fail_open=True,
        )

    user_text = _build_user_message(
        extracted=extracted,
        existing=existing,
        chapter_id=chapter_id,
        chapter_content_snippet=chapter_content_snippet,
    )

    try:
        response = resolved_client.complete(
            messages=[
                Message(role="system", content=system_text),
                Message(role="user", content=user_text),
            ],
            model=resolved_model,
            **complete_kwargs,
        )
        payload = _parse_llm_payload(
            raw_text=response.content, model_used=response.model_used
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        _log.warning(
            "character_disambiguator_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return CharacterDisambiguationResult(
            llm_calls_count=1,
            fail_open=True,
        )

    decisions = [
        CharacterDisambiguationDecision(
            extracted_character_id=d.extracted_character_id,
            target_existing_character_id=d.target_existing_character_id,
            confidence=d.confidence,
            reason=d.reason,
        )
        for d in payload.decisions
    ]

    extracted_ids = {item["character_id"] for item in extracted}
    existing_ids = {ch.character_id for ch in existing}
    valid_decisions = [
        d
        for d in decisions
        if d.extracted_character_id in extracted_ids
        and d.target_existing_character_id in existing_ids
    ]
    if len(valid_decisions) < len(decisions):
        _log.warning(
            "character_disambiguator_invalid_ids_filtered",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            raw_count=len(decisions),
            valid_count=len(valid_decisions),
        )

    if not valid_decisions:
        _log.debug(
            "character_disambiguator_no_match",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            evidence=payload.evidence,
            extracted_count=len(extracted),
            existing_count=len(existing),
        )
    else:
        _log.info(
            "character_disambiguator_match_decided",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            decisions_count=len(valid_decisions),
            decisions=[
                {
                    "extracted": d.extracted_character_id,
                    "target": d.target_existing_character_id,
                    "confidence": d.confidence,
                }
                for d in valid_decisions
            ],
            evidence=payload.evidence,
        )

    return CharacterDisambiguationResult(
        disambiguation_decisions=valid_decisions,
        evidence=payload.evidence,
        llm_calls_count=1,
        skipped=False,
        fail_open=False,
    )


def select_top_k_existing_candidates(
    *,
    extracted: list[dict],
    existing: list[Character],
    top_k: int = _DEFAULT_TOP_K_EXISTING,
) -> list[Character]:
    """从 existing 中筛 top-K 候选(防 LLM token 爆炸 + 兜底描述性指代)。

    双轨筛选策略:
    1. **字面相似度轨**:对每条 extracted,计算它与每条 existing 的 name 相似度
       (``difflib.SequenceMatcher`` ratio),取 max ratio ≥ 0.3 的 existing
    2. **描述性指代轨**(LLM 兜底主战场):existing 中 name 末位含
       ``_DESCRIPTIVE_REFERENCE_SUFFIXES`` 字面("人/官/师/夫/兵/娘/公/婆"等)的
       角色无脑入选 — 真名披露场景字面相似度 ≈ 0 会被轨 1 漏抓

    两轨合并取 union → 字面相似度优先 + 描述性指代池补足 → 取前 K 条。
    """
    import difflib

    if not extracted or not existing:
        return []

    scored: list[tuple[float, Character]] = []
    descriptive_pool: list[Character] = []
    for ch in existing:
        max_ratio = 0.0
        for item in extracted:
            extracted_name = item.get("name", "")
            ratio = difflib.SequenceMatcher(None, ch.name, extracted_name).ratio()
            if ratio > max_ratio:
                max_ratio = ratio
        if max_ratio >= 0.3:
            scored.append((max_ratio, ch))
            continue
        if ch.name and ch.name[-1] in _DESCRIPTIVE_REFERENCE_SUFFIXES:
            descriptive_pool.append(ch)

    scored.sort(key=lambda x: x[0], reverse=True)
    result = [ch for _, ch in scored[:top_k]]
    remaining_slots = top_k - len(result)
    if remaining_slots > 0 and descriptive_pool:
        result.extend(descriptive_pool[:remaining_slots])

    return result


def _build_user_message(
    *,
    extracted: list[dict],
    existing: list[Character],
    chapter_id: str,
    chapter_content_snippet: str = "",
) -> str:
    """拼装 user message:extracted list + existing list + 上下文 + agent_role marker。"""
    extracted_lines = []
    for i, item in enumerate(extracted):
        extracted_lines.append(
            f"  [{i}] character_id={item['character_id']}\n"
            f"      name: {item['name']}\n"
            f"      description: {item.get('description', '')[:200]}"
        )
    extracted_block = "\n".join(extracted_lines)

    existing_lines = []
    for i, ch in enumerate(existing):
        existing_lines.append(
            f"  [{i}] character_id={ch.character_id}\n"
            f"      name: {ch.name}\n"
            f"      archetype: {ch.archetype}\n"
            f"      description: {(ch.appearance + ' | ' + ch.personality)[:200]}"
        )
    existing_block = "\n".join(existing_lines)

    snippet_block = ""
    if chapter_content_snippet:
        snippet_block = (
            "\n## 本章正文片段(供 LLM 判定真名披露场景)\n\n"
            f"{chapter_content_snippet[:1500]}\n"
        )

    body = (
        f"## 本章 (chapter_id={chapter_id}) 新抽取的 character 候选\n\n"
        f"{extracted_block}\n\n"
        "## 工作区已有 character(top-K 候选,字面相似 + 描述性指代双轨筛选)\n\n"
        f"{existing_block}\n"
        f"{snippet_block}"
    )
    return append_role_marker(body, role="character_disambiguator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _DisambiguatorLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = raw_text.strip()
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        text = match.group("body")

    try:
        data = loads_with_quote_repair(
            text,
            agent_name="character_disambiguator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"character_disambiguator LLM 输出非合法 JSON (model={model_used}):"
            f"{exc.msg};raw={text[:200]!r}"
        ) from exc

    try:
        return _DisambiguatorLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"character_disambiguator LLM 输出 schema 不符 (model={model_used}):"
            f"{exc};raw={text[:200]!r}"
        ) from exc
