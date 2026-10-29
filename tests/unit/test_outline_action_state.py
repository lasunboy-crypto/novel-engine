"""High-precision pending/signed overlap checks for structured outline beats."""

from types import SimpleNamespace as Obj

from src.core.rules.outline_action_state import (
    find_pending_signature_overlaps, find_repeated_completed_signatures,
)


def _material(*, choice: str, later_must: str, pending: str):
    earlier = Obj(
        beat_id="beat-11", chapter_range=(11, 11), summary=pending,
        must_advance=[pending],
        dramatic_contract=Obj(actor_id="char-007", choice=choice),
    )
    later = Obj(
        beat_id="beat-12", chapter_range=(12, 12),
        must_advance=[later_must], dramatic_contract=None,
    )
    plans = Obj(volumes=[Obj(
        volume_id="vol-001", arcs=[Obj(beats=[earlier, later])],
    )])
    characters = Obj(characters=[Obj(character_id="char-007", name="卫长川")])
    return plans, characters


def _issues(*, choice: str, later_must: str, pending: str):
    plans, characters = _material(
        choice=choice, later_must=later_must, pending=pending,
    )
    return find_pending_signature_overlaps(
        volume_plans=plans, characters=characters,
    )


def test_same_signature_pending_early_signed_and_later_signed_is_blocked():
    issues = _issues(
        pending="卫长川的折价契拟到落笔前一步，只差药行一句肯接的话",
        choice="卫长川把契推到落笔前一步，先签部分战备地块的转让与门人迁移安置，不把整份卖出去",
        later_must="卫长川签下部分地块转让与门人迁移安置，近半门人迁往云溪中游",
    )
    assert len(issues) == 1
    assert issues[0].earlier_beat_id == "beat-11"
    assert issues[0].later_beat_id == "beat-12"


def test_pending_then_later_signed_once_is_allowed():
    assert not _issues(
        pending="卫长川的折价契拟到落笔前一步，只差药行一句肯接的话",
        choice="卫长川把契推到落笔前一步，决定等药行回话后再签",
        later_must="卫长川签下部分地块转让与门人迁移安置，近半门人迁往云溪中游",
    )


def test_different_signature_objects_are_allowed():
    assert not _issues(
        pending="卫长川的折价契拟到落笔前一步，只差药行一句肯接的话",
        choice="卫长川先签部分战备地块的转让与门人迁移安置",
        later_must="卫长川签下玄铁矿修缮与炼器供货协议",
    )


def test_no_pending_boundary_or_different_signer_is_allowed():
    assert not _issues(
        pending="卫长川整理折价契供人审看",
        choice="卫长川先签部分战备地块的转让与门人迁移安置",
        later_must="卫长川签下部分地块转让与门人迁移安置",
    )
    assert not _issues(
        pending="卫长川的折价契拟到落笔前一步，只差药行一句肯接的话",
        choice="卫长川先签部分战备地块的转让与门人迁移安置",
        later_must="柳承业签下部分地块转让与门人迁移安置",
    )


def test_completed_consequence_repeated_in_next_beat_is_blocked_without_pending():
    early = "纪云舟签下延期收债换减抽的附条件契，药行上游抽走现银，沈砚被视作私自结社"
    later = "纪云舟签下延期收债换减抽的附条件契，条件是覆盖三镇七村取样"
    plans = Obj(volumes=[Obj(volume_id="vol-001", arcs=[Obj(beats=[
        Obj(beat_id="beat-5", chapter_range=(5, 6),
            dramatic_contract=Obj(actor_id="char-001", consequence=early), must_advance=[]),
        Obj(beat_id="beat-7", chapter_range=(7, 8),
            dramatic_contract=None, must_advance=[later]),
    ])])])
    characters = Obj(characters=[Obj(character_id="char-001", name="沈砚"),
                                 Obj(character_id="char-003", name="纪云舟")])
    issues = find_repeated_completed_signatures(volume_plans=plans, characters=characters)
    assert len(issues) == 1
    assert issues[0].earlier_beat_id == "beat-5"
    assert issues[0].later_beat_id == "beat-7"


def test_distinct_contract_or_signer_can_complete_later():
    plans = Obj(volumes=[Obj(volume_id="vol-001", arcs=[Obj(beats=[
        Obj(beat_id="beat-5", chapter_range=(5, 6), dramatic_contract=Obj(
            consequence="纪云舟签下延期收债换减抽的附条件契，双方先离席"), must_advance=[]),
        Obj(beat_id="beat-7", chapter_range=(7, 8), dramatic_contract=None,
            must_advance=["纪云舟签下另一份丹房药材供货契约，双方付款", "叶青禾签下延期收债换减抽的附条件契"]),
    ])])])
    characters = Obj(characters=[Obj(name="纪云舟"), Obj(name="叶青禾")])
    assert not find_repeated_completed_signatures(volume_plans=plans, characters=characters)


def test_v2_pending_signature_guard_uses_facts_and_not_expression_intent():
    plans, characters = _material(
        pending="卫长川的折价契拟到落笔前一步，只差药行一句肯接的话",
        choice="卫长川先签部分地块转让与门人迁移安置",
        later_must="卫长川签下部分地块转让与门人迁移安置",
    )
    drama = plans.volumes[0].arcs[0].beats[0].dramatic_contract
    drama.contract_version = 2
    drama.natural_requirements = lambda: ("卫长川决定待药行同意后再签",)
    assert not find_pending_signature_overlaps(volume_plans=plans, characters=characters)
    drama.natural_requirements = lambda: (drama.choice,)
    assert find_pending_signature_overlaps(volume_plans=plans, characters=characters)


def test_v2_completed_signature_guard_requires_a_factual_event():
    early = "卫长川签下部分地块转让与门人迁移安置，双方收走原件"
    plans, characters = _material(pending="暂未决定", choice="无", later_must=early)
    drama = plans.volumes[0].arcs[0].beats[0].dramatic_contract
    drama.consequence = early
    drama.contract_version = 2
    drama.natural_requirements = lambda: ("卫长川决定暂缓签契",)
    assert not find_repeated_completed_signatures(volume_plans=plans, characters=characters)
    drama.natural_requirements = lambda: (early,)
    assert find_repeated_completed_signatures(volume_plans=plans, characters=characters)
