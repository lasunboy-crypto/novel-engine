"""M1 动态卡控制器 —— 套话台账 + 强化/弱化状态机(v1.1 wave 5 session 1)。

读 dedup 护栏已判定的"套话 padding"信号(``DedupResult.violations``,已滤掉
intentional / motif 回扣 → 天然护留白),跨章累计进**工作区级台账**
(``workspaces/<id>/data/style_controller.json``);某套话在近 ``window`` 章窗口内
复发达 ``threshold_add`` 次 → 标 ``active`` 加入 avoid-list,经 ``active_avoid_list``
注入后续章节卡 prompt(``forbidden_style_phrases`` banner)让 writer 主动规避;连续
``streak_remove`` 章干净 → 撤出。**闭环、自调、上移、护留白。**

详细设计:``docs/plan/m1_dynamic_card_controller_design.md``。

范围铁律(Tier 1):**只做 dedup 单维度**。台账 ``dimension`` 字段固定 ``"dedup"``;
扩 Tier 2(其它护栏信号)留后续 session,每维一个翻译器,不在本模块一上来全维度铺。

复用的现成机制(均已 grep 验真,见设计 §1):
- 信号源 ``DedupResult.violations``(``cross_chapter_dedup_validator.py``)—— 已是
  LLM 判为"注水复读 padding"的短语,intentional / motif 回扣**不进** violations。
- 注入 / 截断 模式镜像 ``_truncate_forbidden_must_achieve_phrases``
  (``chapter_card_service.py``);本控制器走**独立** ``forbidden_style_phrases``
  通道,与 ``forbidden_must_achieve_phrases``(管剧情点同质化)语义不混。
- 持久化原子写镜像 ``core/workspace/meta.py:write_meta``(先写 ``.tmp`` 再 replace)。

**幂等性**(设计 §3 伪代码未覆盖的真实复杂度,实现期 grep 验真后加固):orchestrator
主状态机(``chapter_orchestrator.py`` 的 ``while state not in {APPROVED, ...}`` 循环)
在 rewrite 时会对**同一章**多次重审 → ``record`` 可能对同一 ``chapter_seq`` 多次调用。
故 ``record`` 用 ``chapter_seq > last_chapter_seq`` 单调守卫:重审同章 / 旧章重跑
(``seq <= last_seq``)→ **整体 no-op**,防 ``flagged_chapters`` 重复计数 + ``clean_streak``
重复推进。代价:rewrite 后新出现的套话不在本章捕获,但会在 N+1 章 dedup 复发时抓到
(dedup 逐章跑)+ WARN-only 软机制 → 可接受;换来完全幂等的可推理状态机。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

_log = structlog.get_logger(__name__)

_LEDGER_FILENAME = "style_controller.json"


@dataclass(frozen=True)
class StyleControllerParams:
    """控制器阈值参数(设计 §6 起始值;须 §8 真 LLM 原型长跑校准)。

    - ``window``(K=5):看近几章(对齐 dedup 自身近 5 章窗口)。
    - ``threshold_add``(TH_ADD=2):窗口内被 flag ≥ 此值 → 加入 avoid-list。
      太低 → 误锁正常用词;太高 → 反应慢。
    - ``streak_remove``(M_REMOVE=3):连续 N 章干净 → 撤出。太低 → 震荡;太高
      → avoid-list 不释放。
    - ``cap``(CAP=10):avoid-list 上限(卡有界);太大 → 卡膨胀压 writer。

    滞后(hysteresis)= 防震荡命门:加入用"窗口内 ≥ threshold_add 次"(非撞一次就加),
    撤出用"连续 streak_remove 章干净"(非干净一次就撤),两个不对称阈值间是黏滞区。
    """

    window: int = 5
    threshold_add: int = 2
    streak_remove: int = 3
    cap: int = 10


DEFAULT_PARAMS = StyleControllerParams()


class StylePhraseEntry(BaseModel):
    """单个套话短语的台账条目。"""

    flagged_chapters: list[int] = Field(
        default_factory=list,
        description="该套话被 dedup 判 padding 的全局章序(只留近 window 章窗口内的)。",
    )
    active: bool = Field(
        default=False, description="是否当前在 avoid-list(已注入后续卡)。"
    )
    clean_streak: int = Field(
        default=0, description="连续多少章没再被 flag(驱动弱化撤销)。"
    )


class StyleControllerLedger(BaseModel):
    """工作区级套话台账(落 ``style_controller.json``)。

    控制器是**运行态**(不是工作区身份),故落专用文件而非挂 Workspace 模型 /
    meta.json —— 可独立重置 / 检查,不让身份模型变脏(设计 §2)。
    """

    version: int = 1
    dimension: str = Field(
        default="dedup",
        description="Tier 1 范围铁律:固定 dedup 单维度;Tier 2 扩维留后续 session。",
    )
    phrases: dict[str, StylePhraseEntry] = Field(default_factory=dict)
    last_chapter_seq: int = Field(
        default=0,
        description="最近一次 record 推进到的全局章序(幂等单调守卫锚)。",
    )


def _ledger_path(data_dir: Path) -> Path:
    return data_dir / _LEDGER_FILENAME


def load_ledger(data_dir: Path) -> StyleControllerLedger:
    """读台账。

    - 文件不存在 → 全新空台账(**零回归**:老工作区无文件 = avoid-list 恒空 =
      生成行为与 M1 前 100% 等价)。
    - 文件损坏 / 读失败 → warn + 返回空台账(fail-safe,损坏台账不阻断生成)。
    """
    path = _ledger_path(data_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return StyleControllerLedger()
    except OSError as exc:
        _log.warning(
            "style_controller_ledger_read_failed", path=str(path), error=str(exc)
        )
        return StyleControllerLedger()
    try:
        return StyleControllerLedger.model_validate(json.loads(text))
    except Exception as exc:  # noqa: BLE001 — fail-safe,损坏台账不阻断生成
        _log.warning(
            "style_controller_ledger_corrupt", path=str(path), error=str(exc)
        )
        return StyleControllerLedger()


def save_ledger(data_dir: Path, ledger: StyleControllerLedger) -> None:
    """原子写台账(镜像 ``write_meta``:先写 ``.tmp`` 再 ``Path.replace`` 覆盖)。"""
    path = _ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ledger.model_dump_json(indent=2)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)


def record(
    data_dir: Path,
    chapter_seq: int,
    violations: list[str],
    *,
    params: StyleControllerParams = DEFAULT_PARAMS,
) -> StyleControllerLedger:
    """记一章 dedup 结果进台账,跑强化 / 弱化状态机,落盘,返回更新后台账。

    :param data_dir: 工作区数据目录(``workspaces/<id>/data``);台账落其下。
    :param chapter_seq: 全局章序(``chapter_seq_from_id`` 解析的 ``ch-NNNN`` → int);
        幂等单调守卫锚。
    :param violations: 本章 dedup 判为注水套话的短语(``DedupResult.violations``,
        已滤掉 intentional / motif → 护留白)。**空 list = 本章干净** → 推进所有已知
        短语 ``clean_streak``。
    :param params: 阈值参数(默认 ``DEFAULT_PARAMS``;测试 / 原型校准可注入)。
    :returns: 更新并落盘后的台账(供调用方 / 测试直接断言)。

    **幂等守卫**:``chapter_seq <= last_chapter_seq`` 时整体 no-op(rewrite 重审同章 /
    旧章重跑),防重复计数 + 重复推进 streak(详见模块 docstring)。
    """
    ledger = load_ledger(data_dir)

    # 幂等守卫:仅处理真前进章。重审同章 / 旧章重跑 → no-op。
    if chapter_seq <= ledger.last_chapter_seq:
        return ledger

    flagged = set(violations)

    # 1. flag 本章出现的套话(set 去重 → 幂等)+ 被 flag 的短语 clean_streak 归零。
    for phrase in flagged:
        entry = ledger.phrases.setdefault(phrase, StylePhraseEntry())
        if chapter_seq not in entry.flagged_chapters:
            entry.flagged_chapters.append(chapter_seq)
        entry.clean_streak = 0

    # 2. 本章未出现的已知短语 clean_streak +1(干净累计 → 驱动弱化)。
    for phrase, entry in ledger.phrases.items():
        if phrase not in flagged:
            entry.clean_streak += 1

    # 3. 窗口裁剪:每个短语只留 [seq-window+1, seq] 内的 flag(对齐 dedup 近 N 章)。
    lo = chapter_seq - params.window + 1
    for entry in ledger.phrases.values():
        entry.flagged_chapters = sorted(
            s for s in set(entry.flagged_chapters) if lo <= s <= chapter_seq
        )

    # 4. 强化:窗口内 flag 数 ≥ threshold_add 且未激活 → 激活(加入 avoid-list)。
    for entry in ledger.phrases.values():
        if not entry.active and len(entry.flagged_chapters) >= params.threshold_add:
            entry.active = True

    # 5. 弱化(滞后防震荡):激活中 + 连续 clean_streak ≥ streak_remove → 撤出 avoid-list。
    for entry in ledger.phrases.values():
        if entry.active and entry.clean_streak >= params.streak_remove:
            entry.active = False
            entry.flagged_chapters = []

    # 6. 修剪遗忘:既不活跃又窗口内无 flag 的短语 → 移出台账(保台账有界)。
    ledger.phrases = {
        phrase: entry
        for phrase, entry in ledger.phrases.items()
        if entry.active or entry.flagged_chapters
    }

    # 7. 推进单调锚。
    ledger.last_chapter_seq = chapter_seq

    save_ledger(data_dir, ledger)
    return ledger


def active_avoid_list(
    data_dir: Path,
    *,
    params: StyleControllerParams = DEFAULT_PARAMS,
    workspace_id: str | None = None,
) -> list[str]:
    """返回当前 active 套话清单(经 ``cap`` 截断 → 卡有界),供下一章出卡前注入。

    排序:窗口内 flag 数降序(最严重套话优先保留)→ 短语字典序(确定性 tiebreak)。
    超 ``cap`` → 截断 + 落 ``style_phrases_truncated`` warn(可观测,镜像
    ``chapter_card_forbidden_phrases_truncated``)。台账空 / 无 active → 返回 ``[]``。
    """
    ledger = load_ledger(data_dir)
    active = [(p, e) for p, e in ledger.phrases.items() if e.active]
    active.sort(key=lambda pe: (-len(pe[1].flagged_chapters), pe[0]))
    phrases = [p for p, _ in active]

    if len(phrases) <= params.cap:
        return phrases

    truncated = phrases[: params.cap]
    _log.warning(
        "style_phrases_truncated",
        workspace_id=workspace_id,
        original_count=len(phrases),
        kept_count=len(truncated),
        cap=params.cap,
    )
    return truncated
