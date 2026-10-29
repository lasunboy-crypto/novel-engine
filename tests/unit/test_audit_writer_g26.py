"""G26 rewrite POV 标语锚定测试(v1.1 audit G26 治本)。

G26 = wave 1 末 b1-rerun-deep-audit 治本:writer 在 rewrite mode 下被 v1
fingerprint / validator 失败明细 / 扩展指南分散注意力时,LLM 可能切换主导人称
(luo-clean-v1 ch-5 rerun-5 v4 实证:third_person → first_person 触发 G10
dominant_pov_flipped → rewrite 死循环)。

治本 = 在 rewrite mode 段最顶部加显眼 pov 硬约束标语,让 LLM 第一眼就看到。

本测试为 wave 2-5 重构防御:
- w4-s1 G1 / w4-s2 G2 注入 prompt 段时不能挪掉 G26 标语位置
- w4-s5 I2.2 / w4-s4 I2.1 改 writer prompt / chapter_card 不能误删 G26 字面
- 任何动 _format_rewrite_baseline 的 session 必须保持 G26 段渲染逻辑
"""

from __future__ import annotations

from pathlib import Path

from src.core.agents.writer import _format_rewrite_baseline


# ===========================================================================
# G26:prompt 文件字面契约
# ===========================================================================


class TestG26PromptLiteralAnchor:
    """writer.md / reviser.md 含 G26 字面标识。"""

    @staticmethod
    def _read_prompt(name: str) -> str:
        path = (
            Path(__file__).resolve().parent.parent.parent / "prompts" / name
        )
        return path.read_text(encoding="utf-8")

    def test_writer_py_contains_g26_marker(self) -> None:
        """src/core/agents/writer.py 含 G26 audit 标识(runtime 注入字面所在)。

        G26 标语段是 runtime 由 _format_rewrite_baseline 渲染并注入 user message,
        所以字面在 writer.py 代码里(注释 + 渲染字面),不是写在 writer.md 系统 prompt。
        """
        writer_py = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "core" / "agents" / "writer.py"
        )
        content = writer_py.read_text(encoding="utf-8")
        assert "G26" in content, "src/core/agents/writer.py 必须含 G26 标识"

    def test_writer_md_contains_pov_preserve_anchor(self) -> None:
        """prompts/writer.md rewrite mode 段含'保持同样人称,不要切换'类锚定字面。"""
        content = self._read_prompt("writer.md")
        # 容许多种字面措辞,关键短语必须任一在
        anchors = (
            "保持同样人称",
            "保持原文人称",
            "主导人称保持",
            "不要切换",
            "不切换人称",
        )
        hit = [a for a in anchors if a in content]
        assert hit, (
            f"writer.md 必须含 rewrite 人称保持锚定字面之一: {anchors}; 实测均缺。"
            f"G26 治本核心 — luo-clean-v1 ch-5 rerun-5 v4 现场 dominant_pov_flipped 复发风险。"
        )

    def test_reviser_md_contains_g26_marker(self) -> None:
        """prompts/reviser.md 含 G26 audit 标识(reviser 也接 G26 治理)。"""
        content = self._read_prompt("reviser.md")
        assert "G26" in content, "reviser.md 必须含 G26 标识"

    def test_reviser_md_contains_pov_preserve_anchor(self) -> None:
        """prompts/reviser.md 含人称保持锚定字面。"""
        content = self._read_prompt("reviser.md")
        anchors = (
            "保持原文人称",
            "严格保持原文人称",
            "保持主导人称",
            "不切换",
        )
        hit = [a for a in anchors if a in content]
        assert hit, (
            f"reviser.md 必须含 rewrite/revise 人称保持锚定字面之一: {anchors}"
        )


# ===========================================================================
# G26:_format_rewrite_baseline 渲染契约
# ===========================================================================


def _make_third_person_fingerprint() -> dict[str, float]:
    """三人称主导 fingerprint(他/她 高 + 我/我们 极低)。"""
    fp: dict[str, float] = {
        "paragraph_length_mean": 80.0,
        "sentence_length_mean": 25.0,
        "simile_density": 0.05,
        "total_chars": 3000.0,
        "total_sentences": 100.0,
    }
    for p in ("我", "我们"):
        fp[f"pronoun_freq_{p}"] = 0.001
    for p in ("他", "她", "他们", "她们", "它", "它们"):
        fp[f"pronoun_freq_{p}"] = 0.03
    return fp


def _make_first_person_fingerprint() -> dict[str, float]:
    """一人称主导 fingerprint(我/我们 高 + 他/她 极低)。"""
    fp: dict[str, float] = {
        "paragraph_length_mean": 60.0,
        "sentence_length_mean": 20.0,
        "simile_density": 0.05,
        "total_chars": 3000.0,
        "total_sentences": 100.0,
    }
    for p in ("我", "我们"):
        fp[f"pronoun_freq_{p}"] = 0.06
    for p in ("他", "她", "他们", "她们", "它", "它们"):
        fp[f"pronoun_freq_{p}"] = 0.001
    return fp


class TestG26FormatRewriteBaseline:
    """_format_rewrite_baseline 在 rewrite mode 渲染 G26 pov 标语段。"""

    def test_renders_g26_section_when_fingerprint_present(self) -> None:
        """有 prev_version_fingerprint → 渲染 G26 标语段。"""
        result = _format_rewrite_baseline(
            prev_version_fingerprint=_make_third_person_fingerprint(),
            validator_failures=None,
        )
        assert "G26" in result, "G26 标识必须在 rewrite mode 段渲染输出"
        assert "rewrite 硬约束" in result, "rewrite 硬约束标题必须存在"
        assert "主导人称保持" in result, "主导人称保持锚定字面必须存在"

    def test_g26_section_renders_third_person_label(self) -> None:
        """三人称主导 fingerprint → G26 段渲染 third_person 字面。"""
        result = _format_rewrite_baseline(
            prev_version_fingerprint=_make_third_person_fingerprint(),
            validator_failures=None,
        )
        assert "third_person" in result, "三人称 fingerprint 应渲染 third_person 字面"

    def test_g26_section_renders_first_person_label(self) -> None:
        """一人称主导 fingerprint → G26 段渲染 first_person 字面。"""
        result = _format_rewrite_baseline(
            prev_version_fingerprint=_make_first_person_fingerprint(),
            validator_failures=None,
        )
        assert "first_person" in result, "一人称 fingerprint 应渲染 first_person 字面"

    def test_g26_section_appears_before_v1_fingerprint_data(self) -> None:
        """G26 标语段必须出现在 v1 fingerprint 数字之前。

        治本要点 = "让 LLM 第一眼就看到",位置错乱(挪到 fingerprint 之后)
        等于回到 audit 前的失败状态。
        """
        result = _format_rewrite_baseline(
            prev_version_fingerprint=_make_third_person_fingerprint(),
            validator_failures=None,
        )
        g26_idx = result.find("主导人称保持")
        # v1 风格指纹段标题字面
        fingerprint_idx = result.find("v1 风格指纹")
        assert g26_idx >= 0, "G26 段必须存在"
        assert fingerprint_idx >= 0, "v1 fingerprint 段必须存在"
        assert g26_idx < fingerprint_idx, (
            f"G26 段位置错乱:G26 在字符 {g26_idx},v1 fingerprint 在 {fingerprint_idx}。"
            f"G26 治本要求 G26 段在 fingerprint 之前(LLM 第一眼可见区)。"
        )

    def test_no_g26_section_when_no_fingerprint(self) -> None:
        """无 prev_version_fingerprint → 不渲染 G26 段(无 v1 锚信号源)。"""
        result = _format_rewrite_baseline(
            prev_version_fingerprint=None,
            validator_failures=[{"validator": "word_count", "details": {}}],
        )
        assert "G26" not in result, (
            "无 v1 fingerprint 时不应渲染 G26 段(无 v1 人称信号源)"
        )
        assert "主导人称保持" not in result
