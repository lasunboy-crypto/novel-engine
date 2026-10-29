"""W3.5-s2 模型校准 profile schema + loader 防回退测。

覆盖
====
- ``ModelCalibration`` / ``GlobalCalibration`` / ``GenreCalibration`` schema 校验
  (字段边界 / global alias / 未知原型 key 拒 / extra forbid)。
- ``load_model_calibration`` loader(段缺失优雅空 / 脏数据 fail-fast / 文件不存在抛)。
- ``resolve_genre_archetype`` 题材映射。
- **真 config.yaml 接线测**(按 [[feedback_test_full_wiring_not_just_mechanism_ends]]:
  loader 解析仓库根 config.yaml 的 deepseek 校准,防生产静默失效)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.core.config_loader import load_model_calibration
from src.core.models.model_calibration import (
    GENRE_ARCHETYPES,
    GenreCalibration,
    GlobalCalibration,
    ModelCalibration,
    resolve_genre_archetype,
)

# ---------- 合法样本(供改造测边界)----------

_VALID_GLOBAL = {
    "length_tendency": "overshoot",
    "anti_stamp_strength": "high",
    "recommended_temperature": 0.85,
    "thinking_default": False,
    "reviewer_threshold_adjustment": 0.0,
}
_VALID_GENRE = {
    "chars_per_beat": 1534.0,
    "chars_per_chapter": 4602.0,
    "length_compliance": 0.667,
    "content_units_per_chapter": 3.0,
    "sample_chapters": 6,
}
_VALID_CALIB = {
    "calibrated_at": "2026-06-16",
    "calibration_corpus": "heal-off-v1 + luo-accept-v2 modelab_drafts",
    "global": dict(_VALID_GLOBAL),
    "per_genre": {"introspection": dict(_VALID_GENRE)},
}


def _write_yaml(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return p


# ========== schema ==========


def test_valid_calibration_parses() -> None:
    """合法 payload 解析;``global`` 字面映射到 global_ 字段(alias)。"""
    calib = ModelCalibration.model_validate(_VALID_CALIB)
    assert calib.calibrated_at == "2026-06-16"
    assert calib.global_.length_tendency == "overshoot"
    assert calib.global_.thinking_default is False
    assert calib.per_genre["introspection"].chars_per_beat == 1534.0


def test_global_alias_serializes_back() -> None:
    """by_alias dump 还原 'global' 字面(yaml 往返不丢)。"""
    calib = ModelCalibration.model_validate(_VALID_CALIB)
    dumped = calib.model_dump(by_alias=True)
    assert "global" in dumped
    assert "global_" not in dumped


def test_unknown_archetype_key_rejected() -> None:
    """per_genre 含未知原型 key → ValidationError(防 typo 静默漏校准)。"""
    bad = dict(_VALID_CALIB)
    bad["per_genre"] = {"sci_fi": dict(_VALID_GENRE)}  # 未在 GENRE_ARCHETYPES
    with pytest.raises(ValidationError, match="未知题材原型"):
        ModelCalibration.model_validate(bad)


def test_known_archetypes_are_action_and_introspection() -> None:
    """GENRE_ARCHETYPES 当前 = {action, introspection}(s2 两份语料定义)。"""
    assert GENRE_ARCHETYPES == frozenset({"introspection", "action"})


def test_empty_per_genre_rejected() -> None:
    bad = dict(_VALID_CALIB)
    bad["per_genre"] = {}
    with pytest.raises(ValidationError):
        ModelCalibration.model_validate(bad)


def test_length_compliance_out_of_range_rejected() -> None:
    """length_compliance 必须 ∈ [0,1]。"""
    bad = dict(_VALID_GENRE)
    bad["length_compliance"] = 1.5
    with pytest.raises(ValidationError):
        GenreCalibration.model_validate(bad)


def test_chars_per_beat_must_be_positive() -> None:
    bad = dict(_VALID_GENRE)
    bad["chars_per_beat"] = 0
    with pytest.raises(ValidationError):
        GenreCalibration.model_validate(bad)


def test_temperature_out_of_range_rejected() -> None:
    """recommended_temperature ∈ (0, 2]。"""
    bad = dict(_VALID_GLOBAL)
    bad["recommended_temperature"] = 2.5
    with pytest.raises(ValidationError):
        GlobalCalibration.model_validate(bad)


def test_reviewer_adjustment_out_of_range_rejected() -> None:
    bad = dict(_VALID_GLOBAL)
    bad["reviewer_threshold_adjustment"] = 99.0
    with pytest.raises(ValidationError):
        GlobalCalibration.model_validate(bad)


def test_invalid_length_tendency_literal_rejected() -> None:
    bad = dict(_VALID_GLOBAL)
    bad["length_tendency"] = "huge"  # 非 Literal 三值之一
    with pytest.raises(ValidationError):
        GlobalCalibration.model_validate(bad)


def test_extra_field_forbidden() -> None:
    """extra='forbid':多余字段拒(防 yaml typo 静默忽略)。"""
    bad = dict(_VALID_GENRE)
    bad["chars_per_word"] = 1.0  # 不存在的字段
    with pytest.raises(ValidationError):
        GenreCalibration.model_validate(bad)


# ========== W3.5 校准联动:trim 默认开映射(wants_*,单一真相源)==========


def test_wants_word_count_trim_only_overshoot() -> None:
    """length_tendency==overshoot → 默认开超长 trim(deepseek);balanced/undershoot → 关。"""
    for tendency, expect in (
        ("overshoot", True),
        ("balanced", False),
        ("undershoot", False),
    ):
        g = GlobalCalibration.model_validate(
            {**_VALID_GLOBAL, "length_tendency": tendency}
        )
        assert g.wants_word_count_trim() is expect


def test_wants_anti_tic_trim_only_high() -> None:
    """anti_stamp_strength==high → 默认开 anti-tic trim(deepseek);medium/low → 关
    (收益随 tic 密度走;medium 边际收益不确定,交工作区三态显式覆盖)。"""
    for strength, expect in (("high", True), ("medium", False), ("low", False)):
        g = GlobalCalibration.model_validate(
            {**_VALID_GLOBAL, "anti_stamp_strength": strength}
        )
        assert g.wants_anti_tic_trim() is expect


# ========== genre 映射 ==========


def test_resolve_genre_archetype_known() -> None:
    assert resolve_genre_archetype("治愈日常") == "introspection"
    assert resolve_genre_archetype("都市悬疑") == "action"
    assert resolve_genre_archetype("mystery") == "action"


def test_resolve_genre_archetype_unknown_returns_none() -> None:
    """未映射 genre → None(消费方警告 + 兜底,不静默猜)。"""
    assert resolve_genre_archetype("科幻") is None
    assert resolve_genre_archetype("") is None
    assert resolve_genre_archetype(None) is None


# ========== loader ==========


def test_loader_parses_valid_section(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, {"model_calibration": {"deepseek-v4-pro": dict(_VALID_CALIB)}})
    calib = load_model_calibration(path)
    assert set(calib) == {"deepseek-v4-pro"}
    assert calib["deepseek-v4-pro"].per_genre["introspection"].chars_per_beat == 1534.0


def test_loader_section_absent_returns_empty(tmp_path: Path) -> None:
    """段缺失 = 优雅空 dict(消费方按 model 未命中警告,非崩溃)。"""
    path = _write_yaml(tmp_path, {"writer_profiles": {}})  # 无 model_calibration 段
    assert load_model_calibration(path) == {}


def test_loader_section_not_dict_raises(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, {"model_calibration": ["not", "a", "dict"]})
    with pytest.raises(ValueError, match="model_calibration 段应为 dict"):
        load_model_calibration(path)


def test_loader_model_entry_not_dict_raises(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, {"model_calibration": {"deepseek-v4-pro": "oops"}})
    with pytest.raises(ValueError, match="应为 dict"):
        load_model_calibration(path)


def test_loader_malformed_entry_fail_fast(tmp_path: Path) -> None:
    """段存在但字段坏(未知原型)→ ValidationError(脏数据 fail-fast,非静默)。"""
    bad = dict(_VALID_CALIB)
    bad["per_genre"] = {"bogus": dict(_VALID_GENRE)}
    path = _write_yaml(tmp_path, {"model_calibration": {"m": bad}})
    with pytest.raises(ValidationError):
        load_model_calibration(path)


def test_loader_file_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_model_calibration(tmp_path / "nope.yaml")


# ========== 真 config.yaml 接线(防生产静默失效)==========


def test_repo_config_yaml_has_deepseek_calibration() -> None:
    """仓库根 config.yaml 的 deepseek 校准能被 loader 解析 + 值符合 s2 实测。

    全链路接线测(plan §6-s2 护栏③):loader → 真 config.yaml → ModelCalibration。
    s3 拆章 / s5 路由依赖本段;改坏(字段名/缩进/原型 key)本测红。
    """
    repo_root = Path(__file__).resolve().parents[2]
    calib = load_model_calibration(repo_root / "config.yaml")
    assert "deepseek-v4-pro" in calib, "config.yaml 缺 deepseek-v4-pro 校准段"
    ds = calib["deepseek-v4-pro"]
    # global:deepseek 偏写长 + 需强反盖章 + writer thinking off
    assert ds.global_.length_tendency == "overshoot"
    assert ds.global_.anti_stamp_strength == "high"
    assert ds.global_.thinking_default is False
    # per_genre:两原型齐 + 动作 chars_per_beat > 内省(题材长度反号实证)
    assert set(ds.per_genre) == {"introspection", "action"}
    assert (
        ds.per_genre["action"].chars_per_beat
        > ds.per_genre["introspection"].chars_per_beat
    ), "动作题材 chars_per_beat 应高于内省(w3-s4 题材长度反号)"
    # 动作守闸率低(s3 拆章靶子);内省较高(不触发额外拆)
    assert ds.per_genre["action"].length_compliance < ds.per_genre["introspection"].length_compliance


def test_repo_config_yaml_has_qwen38_length_calibration() -> None:
    """§2.143 新样本只接入长度；未做的文学/Reviewer 项保持中性。"""

    repo_root = Path(__file__).resolve().parents[2]
    calib = load_model_calibration(repo_root / "config.yaml")
    qwen = calib["qwen3.8-max"]

    assert qwen.calibrated_at == "2026-08-16"
    assert qwen.global_.length_tendency == "balanced"
    assert qwen.global_.anti_stamp_strength == "medium"
    assert qwen.global_.thinking_default is True
    assert qwen.global_.reviewer_threshold_adjustment == 0.0
    assert qwen.global_.wants_word_count_trim() is False
    assert qwen.global_.wants_anti_tic_trim() is False
    assert set(qwen.per_genre) == {"introspection", "action"}
    assert qwen.per_genre["introspection"].chars_per_beat == 1352
    assert qwen.per_genre["introspection"].chars_per_chapter == 4056
    assert qwen.per_genre["introspection"].length_compliance == 0.667
    assert qwen.per_genre["action"].chars_per_beat == 1088
    assert qwen.per_genre["action"].chars_per_chapter == 3265
    assert qwen.per_genre["action"].length_compliance == 1.0
