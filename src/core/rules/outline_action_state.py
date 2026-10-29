"""Conservative pending/signed overlap guard for adjacent outline beats.

This intentionally covers only a three-signal signing contradiction. A
possible two-step transaction, a different signer, or a different object must
remain available for the Writer rather than becoming a guessed HARD rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re

from src.core.models.outline import CharactersOutput, VolumePlansOutput


_PENDING = re.compile(r"落笔前|尚未签|未落笔|只差[^。；]{0,30}(?:肯接|落笔)")
_EARLY_SIGNED = re.compile(r"(?:先签|签下|签署|签订)([^，。；;]{4,80})")
_LATER_SIGNED = re.compile(r"(?:签下|签署|签订)([^，。；;]{4,80})")
_COMPLETED_SIGNATURE = re.compile(r"(?:签下|签署|签订)([^，。；;]{8,80})")
_DISTINCT_DEAL = re.compile(r"(?:另一|另外|第二|新的一|再次|重新|另一笔|第二份)")


@dataclass(frozen=True)
class PendingSignatureOverlap:
    volume_id: str
    earlier_beat_id: str
    later_beat_id: str
    actor_name: str
    pending_quote: str
    early_choice_quote: str
    later_must_quote: str

    def feedback(self) -> str:
        return (
            f"卷 {self.volume_id} 的 {self.earlier_beat_id} 与 {self.later_beat_id} "
            f"把同一签约动作同时放在待签、已签和再次签下三个状态："
            f"待签={self.pending_quote!r}；本章选择={self.early_choice_quote!r}；"
            f"后章必达={self.later_must_quote!r}。请明确只在一个 beat 完成签约，"
            "其他 beat 只保留谈判或后果；若确有两份不同契约，必须写清各自标的。"
        )


@dataclass(frozen=True)
class RepeatedCompletedSignature:
    volume_id: str
    earlier_beat_id: str
    later_beat_id: str
    signer: str
    earlier_quote: str
    later_quote: str

    def feedback(self) -> str:
        return (
            f"卷 {self.volume_id} 的 {self.earlier_beat_id} 戏剧后果和 "
            f"{self.later_beat_id} 必达都已写同一人签同一契："
            f"前段={self.earlier_quote!r}；后段={self.later_quote!r}。"
            "请只在一个 Beat 完成该签约；若为两份契约，请明示不同标的。"
        )


def _signature(text: str, names: set[str]) -> tuple[str, str] | None:
    for match in _COMPLETED_SIGNATURE.finditer(text):
        before = text[max(0, match.start() - 14):match.start()]
        signer = next((name for name in sorted(names, key=len, reverse=True)
                       if name in before), None)
        if signer is None:
            continue
        obj = re.sub(r"\s", "", match.group(1))
        if _DISTINCT_DEAL.search(obj):
            continue
        return signer, obj
    return None


def find_repeated_completed_signatures(
    *, volume_plans: VolumePlansOutput, characters: CharactersOutput,
) -> list[RepeatedCompletedSignature]:
    """High-precision same-signer, same-object double completion across beats.

    A dramatic consequence is already completed in its beat.  Requiring the
    same signed object again in a later beat duplicates the action even if no
    earlier pending phrase or contract actor ID matches the actual signer.
    """
    names = {item.name for item in characters.characters}
    issues: list[RepeatedCompletedSignature] = []
    for volume in volume_plans.volumes:
        beats = sorted(
            (beat for arc in volume.arcs for beat in arc.beats),
            key=lambda beat: (beat.chapter_range[0], beat.chapter_range[1]),
        )
        for earlier, later in zip(beats, beats[1:]):
            if earlier.chapter_range[1] >= later.chapter_range[0]:
                continue
            contract = earlier.dramatic_contract
            if contract is None:
                continue
            completed = (contract.natural_requirements() if getattr(contract, "contract_version", 1) == 2
                         else (contract.consequence,))
            found = next(((quote, result) for quote in completed
                          if (result := _signature(quote, names)) is not None), None)
            if found is None:
                continue
            early_quote, early = found
            for later_must in later.must_advance:
                late = _signature(later_must, names)
                if late is None or late[0] != early[0]:
                    continue
                if _DISTINCT_DEAL.search(later_must):
                    continue
                if (early[1] in late[1] or late[1] in early[1]
                        or SequenceMatcher(None, early[1], late[1]).ratio() >= .88):
                    issues.append(RepeatedCompletedSignature(
                        volume_id=volume.volume_id,
                        earlier_beat_id=earlier.beat_id,
                        later_beat_id=later.beat_id,
                        signer=early[0], earlier_quote=early_quote,
                        later_quote=later_must,
                    ))
                    break
    return issues


def _signed_object(text: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    return re.sub(r"[\s的]", "", match.group(1))


def find_pending_signature_overlaps(
    *, volume_plans: VolumePlansOutput, characters: CharactersOutput,
) -> list[PendingSignatureOverlap]:
    """Require exact actor plus close object wording across adjacent beats.

    The check does not infer a deal from an ordinary reference to a contract.
    It requires a stated pending boundary, a completed choice in that same
    beat, and a later must that completes the near-identical object again.
    """
    names = {item.character_id: item.name for item in characters.characters}
    issues: list[PendingSignatureOverlap] = []
    for volume in volume_plans.volumes:
        beats = sorted(
            (beat for arc in volume.arcs for beat in arc.beats),
            key=lambda beat: (beat.chapter_range[0], beat.chapter_range[1]),
        )
        for earlier, later in zip(beats, beats[1:]):
            if earlier.chapter_range[1] >= later.chapter_range[0]:
                continue
            contract = earlier.dramatic_contract
            if contract is None:
                continue
            actor = names.get(contract.actor_id, "")
            choices = (contract.natural_requirements() if getattr(contract, "contract_version", 1) == 2
                       else (contract.choice,))
            found_choice = next(((quote, obj) for quote in choices if actor and actor in quote
                                 and (obj := _signed_object(quote, _EARLY_SIGNED)) is not None), None)
            if found_choice is None:
                continue
            choice, early_object = found_choice
            pending = next(
                (
                    quote for quote in (*earlier.must_advance, earlier.summary)
                    if actor in quote and _PENDING.search(quote)
                ),
                None,
            )
            if pending is None:
                continue
            for later_must in later.must_advance:
                if actor not in later_must:
                    continue
                late_object = _signed_object(later_must, _LATER_SIGNED)
                if late_object is None:
                    continue
                if SequenceMatcher(None, early_object, late_object).ratio() < 0.75:
                    continue
                issues.append(PendingSignatureOverlap(
                    volume_id=volume.volume_id,
                    earlier_beat_id=earlier.beat_id,
                    later_beat_id=later.beat_id,
                    actor_name=actor,
                    pending_quote=pending,
                    early_choice_quote=choice,
                    later_must_quote=later_must,
                ))
                break
    return issues
