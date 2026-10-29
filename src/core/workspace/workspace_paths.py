"""工作区目录布局工具(纯路径计算,不做 IO)。

PROJECT_SPEC §2.4 定义的工作区目录结构::

    workspaces/<workspace_id>/
    ├── project.yaml
    ├── meta.json
    ├── prompts/
    ├── data/
    │   ├── novel.db
    │   ├── chroma/
    │   ├── chapters/<chapter_id>/{v1.md, v2.md, final.md}
    │   ├── cards/
    │   ├── outline/
    │   ├── assets/         # P5-B-08:静态资产源文件(API 上传落盘 + CLI 可选导入源)
    │   └── exports/
    ├── archives/
    ├── backups/
    └── logs/

本模块**只算路径不创建目录**;创建目录的副作用留给 P1-B-14 WorkspaceService。
这样任何模块都能安全 import,不影响文件系统。
"""

from __future__ import annotations

from pathlib import Path

from src.config import settings


class WorkspacePaths:
    """单个工作区的路径集合。

    实例化时绑定 ``workspace_id`` 与 ``workspaces_root``;各路径以 ``@property``
    懒计算,不触发任何 IO。``workspaces_root`` 在 ``__init__`` 内一次性
    ``resolve()``,避免相对路径在下游模块切换 cwd 后产生歧义。
    """

    def __init__(
        self,
        workspace_id: str,
        *,
        workspaces_root: Path | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        root = workspaces_root if workspaces_root is not None else settings.data_dir
        self._root = Path(root).resolve() / workspace_id

    @property
    def root(self) -> Path:
        """工作区根目录,即 ``<workspaces_root>/<workspace_id>``。"""
        return self._root

    @property
    def meta_json(self) -> Path:
        """工作区元数据文件路径。"""
        return self._root / "meta.json"

    @property
    def project_yaml(self) -> Path:
        """工作区配置覆盖文件路径(P3 才用,P1 暂不读写)。"""
        return self._root / "project.yaml"

    @property
    def prompts_dir(self) -> Path:
        """工作区级提示词覆盖目录。"""
        return self._root / "prompts"

    @property
    def data_dir(self) -> Path:
        """工作区数据根目录(novel.db、chroma、chapters 等的容器)。"""
        return self._root / "data"

    @property
    def db_path(self) -> Path:
        """工作区独立 SQLite 文件路径。"""
        return self.data_dir / "novel.db"

    @property
    def chroma_dir(self) -> Path:
        """工作区独立 Chroma 向量库目录(P2 引入,P1 不读写)。"""
        return self.data_dir / "chroma"

    @property
    def chapters_dir(self) -> Path:
        """所有章节版本目录的父目录。"""
        return self.data_dir / "chapters"

    @property
    def cards_dir(self) -> Path:
        """章节卡落盘目录。"""
        return self.data_dir / "cards"

    @property
    def outline_dir(self) -> Path:
        """大纲落盘目录(P5 才用)。"""
        return self.data_dir / "outline"

    @property
    def assets_dir(self) -> Path:
        """静态资产源文件目录(P5-B-08)。

        `POST /api/v1/workspaces/{id}/assets:import` 把 body 里 markdown 内容
        落盘到此目录(``world.md`` / ``characters.md`` 等),再调既有
        ``import_assets`` service 解析入库。文件保留:后续 debug / 浏览器走查 /
        re-import 时可直接看 / 复用源文件,与 CLI ``novel workspace
        import-assets <md_dir>`` 接外部目录互补。
        """
        return self.data_dir / "assets"

    @property
    def exports_dir(self) -> Path:
        """导出产物目录(P6 才用)。"""
        return self.data_dir / "exports"

    @property
    def archives_dir(self) -> Path:
        """prompts/responses 归档目录(参 OBSERVABILITY)。"""
        return self._root / "archives"

    @property
    def backups_dir(self) -> Path:
        """自动 / 手动备份目录。"""
        return self._root / "backups"

    @property
    def logs_dir(self) -> Path:
        """工作区日志目录。"""
        return self._root / "logs"

    def chapter_dir(self, chapter_id: str) -> Path:
        """单个章节的版本目录,如 ``data/chapters/v1c012/``。"""
        return self.chapters_dir / chapter_id

    def chapter_version_file(self, chapter_id: str, filename: str) -> Path:
        """单个章节版本文件,如 ``data/chapters/v1c012/v1.md``。

        ``filename`` 由调用方约定(P1-B-05 章节版本管理负责),本模块只拼路径。
        """
        return self.chapter_dir(chapter_id) / filename
