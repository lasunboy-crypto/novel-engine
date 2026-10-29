"""LLM-based 记忆重排(P3-B-08 引入)。

接收 retriever(P2-B-08)产出的 top_K(默认 20)候选记忆与当前章节卡上下
文,调注入的 ``LLMClient`` 让 LLM 按"与本章相关度"重排,返回 top_N(默认
5)条记忆,供下游 Writer 拼装"## 检索记忆"段时只取最相关的少数条目,降低
prompt 噪声。

为什么这次做
=============

P2 retriever 走向量距离 top_K=20 → 噪声较大,不同 entity_type / 不同章节
时间维度的记忆都可能混在一起,Writer 拼装时段塞 20 条会挤占正文写作字数预
算(每条 100-300 字 → 4000-12000 字仅检索段)。P3-plan 决策 4 把 reranker
落位为 LLM-based 精排:用语义判断剔除明显与本章无关的命中,留 top_N=5 条。

设计要点
========

1. **注入式 ``ModelRouter`` 主路径,legacy ``client + model`` 兜底**:对齐
   P3-B-06 全 agent 改造模板,reranker 走 ``router.route("reranker")``
   取 profile / client / 生成参数;``router=None`` 时退化到 legacy
   ``client + model`` + 调用方自定义 ``temperature`` / ``max_tokens``
   路径(供既有单测无 router 注入也能跑)。
2. **候选数 ≤ ``top_n`` 时跳过 LLM 调用**:LLM 不做无意义的"5→5 重排",
   直接返原列表(retriever 已按距离升序)。这条短路省一次 LLM 调用,
   ``top_n`` 默认 5 时小工作区(向量库 < 10 条记忆)很常见。
3. **JSON 输出契约 ``{"ranking": [memory_id, ...]}``**:LLM 只输出 ID 列
   表,不复读全文。解析失败 / LLM 异常 / LLM 漏选某些 ID → fallback 返
   ``memories[:top_n]``(按原距离升序),并落 ``reranker_fallback`` 一条
   structlog warn 不抛——重排是"提质"层,LLM 失败时退化为原 distance 排
   序仍可用;让本任务整体 raise 反而更糟。
4. **LLM 漏选 / 多选 ID 的兜底**:LLM 返的 ID 序列可能不是原候选的子集
   (幻觉新 ID)或漏选部分 ID;按"先用 LLM 顺序,再按原距离顺序补足"组
   装,最终截 ``top_n``。多余的幻觉 ID 直接忽略(在原候选 dict 里查不到
   memory_id 即丢弃)。
5. **prompt 拼装上下文最简**:user message 含两段——"## 当前章节卡上下
   文"(主线 / 视角 / 场景 / 张力)+ "## 待排序记忆候选"(编号 + memory_id
   + 内容前 200 字截断)。章节卡硬约束三组列表(必达 / 未解 / 禁止)不
   全量塞入,只挑 ``must_achieve`` 关键事件(伏笔级别,与"找相关记忆"语
   义近)。
6. **不修改 retriever / writer 调用栈**:本任务交付 reranker 函数,wiring
   到 ``writer.py:_build_retrieved_memory_section`` 留给正文写作或
   后续微任务一并落。

边界
====

- ``memories`` 空列表 → 直接返空(无 LLM 调用)。
- ``top_n <= 0`` → 抛 ``RerankerError`` 与 retriever ``RetrieverError``
  语义对齐。
- ``router=None`` 且 ``client`` / ``model`` 任一缺 → ``resolve_routing``
  抛 ``TypeError`` 自然冒泡。
- LLM 输出非合法 JSON / JSON 不含 ``ranking`` 字段 / ``ranking`` 非列表 →
  fallback 返 ``memories[:top_n]`` + structlog warn,不抛。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

from src.config import settings
from src.core.agents._prompt import load_prompt_template, resolve_routing
from src.core.llm.base import LLMClient, Message
from src.core.memory.retriever import RetrievedMemory
from src.core.models.chapter_card import ChapterCard
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_RERANKER_PROMPT_FILENAME = "reranker.md"

# Reranker 默认生成参数:重排是结构化输出,温度低;输出只装一份 JSON,
# 不需长文本(top_n=5 + memory_id 字符串 < 500 token)。
_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_MAX_TOKENS = 800

# 默认精排目标条数(对齐 P3-plan §"P3-B-08:Reranker top_20 → top_5")。
DEFAULT_TOP_N = 5

# 单条记忆候选展示给 LLM 的内容截断长度。每条 < 200 字 → 20 候选 ≈ 4000
# 字 prompt body,够窗口(local_qwen_safe context_window=16000)。
_CANDIDATE_CONTENT_PREVIEW_CHARS = 200

# 复用 reviewer / memory_extractor 的代码块 + trailing 注释剥离正则。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


class RerankerError(ValueError):
    """reranker 入参非法时抛出(与 ``retriever.RetrieverError`` 语义对齐)。

    LLM 解析失败 / 调用异常**不抛**本错(走 fallback);本错只在调用方传
    入非法参数(``top_n <= 0`` 等)时抛,让调用方早 fail。
    """


def rerank_memories(
    memories: list[RetrievedMemory],
    chapter_card: ChapterCard,
    *,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> list[RetrievedMemory]:
    """LLM 重排候选记忆,返 top_N。

    流程:
    1. ``len(memories) <= top_n`` → 直接返(无 LLM 调用)。
    2. 否则按 ``router`` / ``client + model`` 解析路由,组 prompt,调 LLM。
    3. 解析 LLM 输出 ``{"ranking": [memory_id, ...]}``;按 ranking 顺序重排
       原 memories 列表,LLM 漏选的 memory_id 按原 distance 顺序补位。
    4. 截前 ``top_n`` 返。
    5. 任一步失败 → fallback ``memories[:top_n]`` + structlog warn。

    :param memories: 候选记忆列表(通常来自 :func:`retrieve_memories_for_chapter`
        的 top_K=20 输出)。空列表直接返空。
    :param chapter_card: 当前章节卡;LLM 重排时拿到的查询语境(主线 / 视角
        / 场景 / 张力)。
    :param router: P3-B-06 主路径。给定时按 ``router.route("reranker")``
        取 client + model + 生成参数;``router`` 与 ``client`` 二选一非空。
    :param client: legacy 路径 LLM 客户端。
    :param model: legacy 路径模型名。
    :param top_n: 精排后保留的记忆条数,默认 5(对齐 P3-plan §P3-B-08
        字面"top_20 → top_5")。``top_n <= 0`` 抛 :class:`RerankerError`。
    :param max_tokens: legacy 路径输出 token 上限,默认 800。``router`` 路径
        下由 profile 决定。
    :param temperature: legacy 路径采样温度,默认 0.2。``router`` 路径下由
        profile 决定。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``list[RetrievedMemory]``,最多 ``top_n`` 条;按 LLM 重排顺序
        给出,LLM 失败 fallback 时按原 distance 升序。
    :raises RerankerError: ``top_n <= 0``。
    :raises TypeError: ``router=None`` 且 ``client`` / ``model`` 任一缺。
    """
    if top_n <= 0:
        raise RerankerError(f"top_n 必须为正整数,实际 {top_n}")

    if not memories:
        return []

    # 候选数 ≤ top_n:LLM 调用无意义,短路返。
    if len(memories) <= top_n:
        return list(memories)

    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name="reranker",
        router=router,
        client=client,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    system_text = load_prompt_template(template_root / _RERANKER_PROMPT_FILENAME)

    user_text = _build_user_message(
        chapter_card=chapter_card,
        memories=memories,
        top_n=top_n,
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
    except Exception as exc:  # noqa: BLE001 - reranker 是"提质"层,LLM 失败 fallback 不抛
        _log.warning(
            "reranker_fallback",
            reason="llm_call_failed",
            chapter_id=chapter_card.chapter_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            candidate_count=len(memories),
            top_n=top_n,
        )
        return list(memories[:top_n])

    ranking = _parse_ranking(
        raw_text=response.content,
        chapter_id=chapter_card.chapter_id,
        candidate_count=len(memories),
    )
    if ranking is None:
        return list(memories[:top_n])

    reordered = _reorder_by_ranking(memories=memories, ranking=ranking)

    _log.info(
        "reranker_completed",
        chapter_id=chapter_card.chapter_id,
        candidate_count=len(memories),
        top_n=top_n,
        ranking_size=len(ranking),
        reranker_model=response.model_used,
    )

    return reordered[:top_n]


# ---- 内部辅助 ---------------------------------------------------------------


def _build_user_message(
    *,
    chapter_card: ChapterCard,
    memories: list[RetrievedMemory],
    top_n: int,
) -> str:
    """user 消息:章节卡查询语境 + 候选记忆列表 + 输出指令。

    候选段:每条形如 ``- [memory_id] <content 前 200 字>``;LLM 按
    memory_id 输出排序。""按原 distance 升序展示" 让 LLM 也能感知向量距
    离(理论上 LLM 不必知道距离,但前置展示让 LLM 先看到检索器认为最近
    的几条)。
    """
    must_achieve_text = (
        " | ".join(chapter_card.must_achieve) if chapter_card.must_achieve else "无"
    )

    context_block = (
        "## 当前章节卡上下文\n\n"
        f"- 视角角色(pov_character):{chapter_card.pov_character}\n"
        f"- 场景设置(setting):{chapter_card.setting}\n"
        f"- 张力等级(tension_level):{chapter_card.tension_level}/10\n"
        f"- 主线必达事件(must_achieve):{must_achieve_text}\n"
        f"- 章末种子(next_chapter_seed):{chapter_card.next_chapter_seed}"
    )

    candidate_lines: list[str] = []
    for hit in memories:
        preview = hit.content.strip().replace("\n", " ")
        if len(preview) > _CANDIDATE_CONTENT_PREVIEW_CHARS:
            preview = preview[:_CANDIDATE_CONTENT_PREVIEW_CHARS] + "…"
        candidate_lines.append(f"- [{hit.memory_id}] {preview}")
    candidate_block = (
        f"## 待排序记忆候选(共 {len(memories)} 条)\n\n"
        + "\n".join(candidate_lines)
    )

    instruction_block = (
        f"## 输出要求\n\n"
        f"从上方候选中选出与本章**最相关**的 {top_n} 条记忆 ID,按相关度从"
        "高到低排序,只输出一份 JSON,不要任何前后缀文字、不要 markdown "
        "代码块包裹。格式:\n\n"
        '```json\n{"ranking": ["memory_id_1", "memory_id_2", ...]}\n```\n\n'
        "memory_id 必须**严格**取自候选列表方括号内的字面值;不要凭空生造。"
    )

    return "\n\n".join([context_block, candidate_block, instruction_block])


def _parse_ranking(
    *,
    raw_text: str,
    chapter_id: str,
    candidate_count: int,
) -> list[str] | None:
    """剥代码块 → ``json.loads`` → 取 ``ranking`` 字段(必须是 ``list[str]``)。

    任一失败返 ``None`` + structlog ``reranker_fallback`` warn(不抛)。
    """
    text = _strip_code_fence(raw_text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning(
            "reranker_fallback",
            reason="invalid_json",
            chapter_id=chapter_id,
            candidate_count=candidate_count,
            json_error=str(exc),
            raw_output=raw_text,
        )
        return None

    if not isinstance(data, dict) or "ranking" not in data:
        _log.warning(
            "reranker_fallback",
            reason="missing_ranking_field",
            chapter_id=chapter_id,
            candidate_count=candidate_count,
            raw_output=raw_text,
        )
        return None

    ranking = data["ranking"]
    if not isinstance(ranking, list) or not all(
        isinstance(item, str) for item in ranking
    ):
        _log.warning(
            "reranker_fallback",
            reason="ranking_not_list_of_str",
            chapter_id=chapter_id,
            candidate_count=candidate_count,
            raw_output=raw_text,
        )
        return None

    return ranking


def _reorder_by_ranking(
    *,
    memories: list[RetrievedMemory],
    ranking: list[str],
) -> list[RetrievedMemory]:
    """按 LLM 给的 ID 顺序重排;LLM 漏选的按原 distance 顺序补位,幻觉 ID
    丢弃。

    实现:
    1. 用 ``memory_id → RetrievedMemory`` 字典快速查;
    2. 顺次走 ``ranking``,命中字典即追加到 ``ordered`` 并从字典 pop;
    3. 字典剩余项(LLM 漏选)按原 ``memories`` 列表顺序追加(retriever 已
       按 distance 升序)。
    """
    by_id: dict[str, RetrievedMemory] = {hit.memory_id: hit for hit in memories}
    ordered: list[RetrievedMemory] = []

    for memory_id in ranking:
        hit = by_id.pop(memory_id, None)
        if hit is not None:
            ordered.append(hit)
        # else: LLM 幻觉 ID,直接丢弃

    # 漏选的 memory_id 按原列表顺序追加(retriever 已按 distance 升序)。
    for hit in memories:
        if hit.memory_id in by_id:
            ordered.append(hit)
            del by_id[hit.memory_id]

    return ordered


def _strip_code_fence(text: str) -> str:
    """剥掉 trailing HTML 注释 + ``` / ```json 包裹。

    与 reviewer / memory_extractor 同源逻辑,LLM 输出格式杂质统一兜底。
    """
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text
