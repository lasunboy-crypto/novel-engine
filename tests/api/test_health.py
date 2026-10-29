"""健康检查路由 + 包络形态用例(P1-B-17)。

覆盖三类断言:

- ``GET /api/v1/system/health`` 返回 200 + 成功包络 + 引擎版本字段。
- 响应严格符合 API_CONTRACT §3.1:顶层只有 ``data`` / ``meta`` 两个键(本路由
  不分页,``meta=None`` 由 ``exclude_none=True`` 不出现于 JSON 中——但因
  默认序列化保留 ``meta=null``,本用例只断言 ``data`` 存在 + 顶层无 ``error``
  / ``ok`` 键)。
- ``X-Request-ID`` 响应头存在且形如 ``req_<hex12>``;请求带自定义
  ``X-Request-ID`` 时回显原值(便于跨进程 trace 串联)。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.version import ENGINE_VERSION


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


class TestHealth:
    def test_returns_ok_envelope_with_engine_version(self, client: TestClient) -> None:
        resp = client.get("/api/v1/system/health")
        assert resp.status_code == 200
        body = resp.json()

        assert "data" in body, "成功包络必须含 data"
        assert "error" not in body, "成功响应不得含 error 键(API_CONTRACT §3.1)"
        assert "ok" not in body, "API_CONTRACT 字面无 ok 字段"

        assert body["data"]["status"] == "ok"
        assert body["data"]["engine_version"] == ENGINE_VERSION

    def test_request_id_header_generated_when_absent(self, client: TestClient) -> None:
        resp = client.get("/api/v1/system/health")
        request_id = resp.headers.get("X-Request-ID")
        assert request_id is not None, "中间件必须回显 X-Request-ID"
        assert re.fullmatch(r"req_[0-9a-f]{12}", request_id), (
            f"格式必须是 req_<hex12>(OBSERVABILITY §2.3),实际 {request_id!r}"
        )

    def test_request_id_echoed_when_provided(self, client: TestClient) -> None:
        custom = "req_ffffffffffff"
        resp = client.get(
            "/api/v1/system/health",
            headers={"X-Request-ID": custom},
        )
        assert resp.headers["X-Request-ID"] == custom


class TestOpenAPI:
    """Swagger UI 路径与 OpenAPI 文档可访问性。"""

    def test_openapi_schema_includes_health(self, client: TestClient) -> None:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "/api/v1/system/health" in schema["paths"]

    def test_swagger_ui_available(self, client: TestClient) -> None:
        resp = client.get("/docs")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
