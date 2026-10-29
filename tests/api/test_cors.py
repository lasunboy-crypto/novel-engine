"""CORS 中间件用例(P1-F-01)。

覆盖断言:

- 白名单 origin 的预检 ``OPTIONS`` 响应包含 ``access-control-allow-origin``
  / ``access-control-allow-headers``(显式含 ``X-Workspace-Id``)/
  ``access-control-allow-methods`` / ``access-control-allow-credentials``。
- 白名单 origin 的实际 ``GET`` 请求响应也带 ``access-control-allow-origin``
  与 ``access-control-allow-credentials``。
- 非白名单 origin 不回 ``access-control-allow-origin``(浏览器据此拒绝)。

测试不依赖真实浏览器,直接用 FastAPI ``TestClient`` 发带 ``Origin`` /
``Access-Control-Request-*`` 头的请求,验证响应头。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


class TestCorsPreflight:
    """预检 OPTIONS 请求:浏览器先发 OPTIONS,后端必须许可才能继续真请求。"""

    def test_preflight_allows_x_workspace_id_header(self, client: TestClient) -> None:
        """X-Workspace-Id 是 P1-B-19 自定义 header,必须在 allow-headers 列。"""
        response = client.options(
            "/api/v1/chapters/ch-0001:write",
            headers={
                "Origin": "http://localhost:5174",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Workspace-Id, Content-Type",
            },
        )
        assert response.status_code == 200
        allow_headers = response.headers.get("access-control-allow-headers", "")
        # 大小写不敏感:浏览器对 header 名不区分大小写,starlette 会回小写。
        assert "x-workspace-id" in allow_headers.lower()
        assert "content-type" in allow_headers.lower()

    def test_preflight_returns_allow_origin_for_5174(self, client: TestClient) -> None:
        response = client.options(
            "/api/v1/system/health",
            headers={
                "Origin": "http://localhost:5174",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert (
            response.headers.get("access-control-allow-origin")
            == "http://localhost:5174"
        )
        assert response.headers.get("access-control-allow-credentials") == "true"

    def test_preflight_returns_allow_origin_for_5173(self, client: TestClient) -> None:
        """5173 是主仓库 vite 端口,也应在白名单。"""
        response = client.options(
            "/api/v1/system/health",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert (
            response.headers.get("access-control-allow-origin")
            == "http://127.0.0.1:5173"
        )

    def test_preflight_advertises_required_methods(self, client: TestClient) -> None:
        response = client.options(
            "/api/v1/system/health",
            headers={
                "Origin": "http://localhost:5174",
                "Access-Control-Request-Method": "PUT",
            },
        )
        allow_methods = response.headers.get("access-control-allow-methods", "").upper()
        for verb in ("GET", "POST", "PUT", "DELETE", "OPTIONS"):
            assert verb in allow_methods, f"{verb} should be in allow-methods"


class TestCorsActualRequest:
    """实际请求(非预检)的 CORS 头注入。"""

    def test_get_with_allowed_origin_includes_acao(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/system/health",
            headers={"Origin": "http://localhost:5174"},
        )
        assert response.status_code == 200
        assert (
            response.headers.get("access-control-allow-origin")
            == "http://localhost:5174"
        )

    def test_get_without_origin_succeeds_without_acao(self, client: TestClient) -> None:
        """非浏览器请求(无 Origin 头)正常通过,响应不带 ACAO 头。"""
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers


class TestCorsRejectsForeignOrigin:
    """白名单外 origin 不回 ACAO 头,浏览器侧据此拒绝。"""

    def test_disallowed_origin_no_acao(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/system/health",
            headers={"Origin": "http://evil.example.com"},
        )
        # 后端不会主动 4xx 拒绝,只是不回 ACAO,浏览器自己拦截。
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers

    def test_disallowed_origin_preflight_no_acao(self, client: TestClient) -> None:
        response = client.options(
            "/api/v1/system/health",
            headers={
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in response.headers
