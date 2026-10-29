"""P1 文件式记忆:把工作区 ``data/world.md`` 与 ``data/characters.md`` 拼成
Writer prompt 用的 memory section 文本。

职责边界(P1-B-06):
  - 只读不写;不创建目录、不维护索引、不缓存。
  - 文件不存在或内容空白 → 视同"该段记忆缺失",**不抛错**(记忆缺失对 Writer
    是 warning 不是 error,模型仍能继续生成,只是缺背景)。
  - 两个文件都缺 → 返回 ``""``(空字符串),由调用方决定是否在 prompt 里提示
    "无背景信息可用"。
  - prompt 拼装格式 P1 简版:``## 世界观`` / ``## 人物`` 两段 markdown 标题 +
    正文。Writer agent(P1-B-10)若要改格式由那边覆盖,本模块不预留参数化。

P2 引入 Chroma 向量记忆后,本模块会与 ``vector_memory.py`` 并存于 ``memory/``
子包下;调用方按检索策略选择走哪条路径。
"""

from __future__ import annotations

from pathlib import Path

from src.core.workspace.workspace_paths import WorkspacePaths

_WORLD_FILENAME = "world.md"
_CHARACTERS_FILENAME = "characters.md"

_WORLD_HEADING = "## 世界观"
_CHARACTERS_HEADING = "## 人物"


def _read_section(path: Path, heading: str) -> str | None:
    """读取一个记忆文件,拼成 ``<heading>\\n\\n<content>``。

    - 文件不存在 → ``None``(交由上层判定整段是否输出)。
    - 文件存在但 ``strip()`` 后为空 → ``None``(等同缺失)。
    - 正常情况下返回拼好的段落字符串(末尾不带换行)。
    """

    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return None
    return f"{heading}\n\n{content}"


def build_memory_section(paths: WorkspacePaths) -> str:
    """读取工作区两份记忆文件,拼成 Writer prompt 的 memory section 文本。

    返回值形态:

    - 两段齐全 → ``"## 世界观\\n\\n<...>\\n\\n## 人物\\n\\n<...>"``
    - 只有一段 → 仅含该段(标题 + 内容)
    - 两段都缺 → ``""``(空字符串,不抛错)

    缺失语义包含 3 种情况:文件不存在、``data_dir`` 不存在、文件内容为纯空白。
    """

    data_dir = paths.data_dir
    world_section = _read_section(data_dir / _WORLD_FILENAME, _WORLD_HEADING)
    characters_section = _read_section(
        data_dir / _CHARACTERS_FILENAME, _CHARACTERS_HEADING
    )

    sections = [s for s in (world_section, characters_section) if s is not None]
    return "\n\n".join(sections)
