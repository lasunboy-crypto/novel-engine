"""I1(v1.1 wave 4 session 3):GET /api/v1/style-templates 路由集成测试。

覆盖端点返回 6 个预设模板(元数据 + 正文预览 + 10 维度),顺序稳定。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_list_returns_six_templates_in_order(client: TestClient) -> None:
    resp = client.get("/api/v1/style-templates")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [t["name"] for t in data] == [
        "comedic_burst",
        "grand_epic",
        "heroic_battle",
        "dark_mystery",
        "healing_daily",
        "classical_xianxia",
        "xianxia_growth",
    ]


def test_each_template_has_meta_dimensions_preview(client: TestClient) -> None:
    data = client.get("/api/v1/style-templates").json()["data"]
    for t in data:
        assert t["label"].strip()
        assert t["description"].strip()
        assert len(t["dimensions"]) == 10
        assert all(v.strip() for v in t["dimensions"].values())
        # 预览正文非空 + 是对应模板的风格指南。
        assert t["preview"].strip()
        assert "写作风格指南" in t["preview"]


def test_comedic_preview_content(client: TestClient) -> None:
    data = client.get("/api/v1/style-templates").json()["data"]
    comedic = next(t for t in data if t["name"] == "comedic_burst")
    assert "搞笑爽文" in comedic["preview"]
    assert comedic["dimensions"]["genre_tone"] == "搞笑"
