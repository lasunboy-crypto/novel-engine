"""错误体契约用例(P1-B-17)。

覆盖三条路径:

- 路由 ``raise ApiError`` 子类 → 严格 §3.2 形态,``error.code`` /
  ``error.message`` / ``error.request_id`` 必齐。
- 422 Pydantic 校验失败 → 错误码 ``VALIDATION_FAILED``,``details.errors``
  含 Pydantic 错误数组。
- 兜底 ``Exception`` → 响应给 ``INTERNAL_ERROR`` 通用消息(不暴露 traceback);
  service 层异常翻译路径同样走这条 handler,验证 ``WorkspaceServiceError``
  / ``MetaFileError`` / ``ChapterServiceError`` / ``ChapterVersioningError`` 各自
  翻译到的 code 与 http_status。

构造受测 app 时不直接复用 ``src.api.main.create_app``,而是基于它再追加几个
专门用于触发各类异常的临时路由——避免污染生产 app 路由表,也确保用例间互不
干扰。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api.errors import (
    ApiErrorTranslationMiddleware,
    ChapterCardContinuityBridgeRequired,
    ChapterCardContinuityStale,
    ChapterCardInvalid,
    ChapterInvalidState,
    ChapterNotFound,
    ChapterVersionConflict,
    ContinuityLedgerNotReady,
    ProviderCallCapReached,
    WorkspaceConflict,
    WorkspaceInvalid,
    WorkspaceNotFound,
    _translate_service_error,
    register_exception_handlers,
)
from src.api.main import create_app
from src.api.middleware import AccessLogMiddleware, RequestIDMiddleware
from src.core.chapter_versioning import ChapterVersioningError
from src.core.workspace.meta import MetaFileError
from src.services.chapter_card_service import (
    CardContinuityBridgeRequiredError,
    CardContinuityLedgerStaleError,
)
from src.services.chapter_rewind_service import (
    ForcePassContinuityLedgerNotReadyError,
)
from src.services.chapter_service import (
    ChapterCardContinuityBridgeError,
    ChapterCardContinuityStaleError,
    ChapterServiceError,
    ContinuityLedgerNotReadyError,
)
from src.services.attempt_ledger import ProviderCallCapExceeded
from src.services.outline_service import OutlineLengthCalibrationRequired
from src.services.profile_override import ProfileNotFoundError
from src.services.workspace_service import WorkspaceServiceError


class _Body(BaseModel):
    name: str
    age: int


def _build_app_with_test_routes() -> FastAPI:
    """在生产 app 上挂几条用于触发异常的临时路由。"""

    app = create_app()

    @app.get("/_t/api_error")
    async def _api_error_route() -> None:
        raise WorkspaceNotFound("工作区不存在: scifi_book_1")

    @app.get("/_t/api_error_with_details")
    async def _api_error_with_details() -> None:
        raise ChapterInvalidState(
            "状态不允许 :write",
            details={"current_state": "approved"},
        )

    @app.post("/_t/validate")
    async def _validate(body: _Body) -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/_t/unknown")
    async def _unknown() -> None:
        raise RuntimeError("意料之外的崩溃")

    @app.get("/_t/svc_workspace")
    async def _svc_workspace() -> None:
        raise WorkspaceServiceError("工作区已归档")

    @app.get("/_t/svc_meta")
    async def _svc_meta() -> None:
        raise MetaFileError("meta.json 解析失败")

    @app.get("/_t/svc_chapter_not_found")
    async def _svc_chapter_not_found() -> None:
        raise ChapterServiceError("章节不存在: v1c012")

    @app.get("/_t/svc_chapter_card")
    async def _svc_chapter_card() -> None:
        raise ChapterServiceError("章节卡 JSON 不合法: 缺字段 chapter_id")

    @app.get("/_t/svc_chapter_other")
    async def _svc_chapter_other() -> None:
        raise ChapterServiceError("章节正在写作中,不允许 :revise")

    @app.get("/_t/svc_versioning")
    async def _svc_versioning() -> None:
        raise ChapterVersioningError("v3 已存在")

    @app.get("/_t/svc_profile_not_found")
    async def _svc_profile_not_found() -> None:
        raise ProfileNotFoundError("profile_id='p_unknown' 未在 writer_profiles 声明")

    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_build_app_with_test_routes())


def _assert_envelope_shape(body: dict) -> None:
    """共用断言:错误响应形态严格对齐 API_CONTRACT §3.2。"""

    assert "error" in body
    assert "data" not in body
    assert "meta" not in body
    err = body["error"]
    assert "code" in err
    assert "message" in err
    assert "request_id" in err
    assert err["request_id"].startswith("req_")


class TestApiErrorHandler:
    def test_404_workspace_not_found(self, client: TestClient) -> None:
        resp = client.get("/_t/api_error")
        assert resp.status_code == 404
        body = resp.json()
        _assert_envelope_shape(body)
        assert body["error"]["code"] == "WORKSPACE_NOT_FOUND"
        assert body["error"]["message"] == "工作区不存在: scifi_book_1"

    def test_details_passed_through(self, client: TestClient) -> None:
        resp = client.get("/_t/api_error_with_details")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "CHAPTER_INVALID_STATE"
        assert body["error"]["details"] == {"current_state": "approved"}


class TestValidationHandler:
    def test_pydantic_422(self, client: TestClient) -> None:
        resp = client.post("/_t/validate", json={"name": "alice"})  # missing age
        assert resp.status_code == 422
        body = resp.json()
        _assert_envelope_shape(body)
        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "errors" in body["error"]["details"]
        assert isinstance(body["error"]["details"]["errors"], list)
        assert len(body["error"]["details"]["errors"]) >= 1


class TestHttpExceptionHandler:
    """starlette ``HTTPException``(404 / 405 / 等)统一翻译为 ApiError 包络。"""

    def test_unknown_route_returns_route_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/__nope__")
        assert resp.status_code == 404
        body = resp.json()
        _assert_envelope_shape(body)
        assert body["error"]["code"] == "ROUTE_NOT_FOUND"

    def test_method_not_allowed(self, client: TestClient) -> None:
        # health 路由只接 GET,DELETE 触发 405。
        resp = client.delete("/api/v1/system/health")
        assert resp.status_code == 405
        body = resp.json()
        _assert_envelope_shape(body)
        assert body["error"]["code"] == "METHOD_NOT_ALLOWED"


class TestUnhandledExceptionHandler:
    def test_runtime_error_returns_internal_error_without_traceback(
        self, client: TestClient
    ) -> None:
        resp = client.get("/_t/unknown")
        assert resp.status_code == 500
        body = resp.json()
        _assert_envelope_shape(body)
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert body["error"]["message"] == "服务器内部错误"
        # traceback 不应回到响应中;message + code 应是通用兜底文本。
        text = resp.text
        assert "RuntimeError" not in text
        assert "意料之外的崩溃" not in text


class TestServiceErrorTranslation:
    def test_workspace_service_error_to_409(self, client: TestClient) -> None:
        resp = client.get("/_t/svc_workspace")
        assert resp.status_code == WorkspaceConflict.http_status == 409
        assert resp.json()["error"]["code"] == "WORKSPACE_CONFLICT"

    def test_meta_file_error_to_422(self, client: TestClient) -> None:
        resp = client.get("/_t/svc_meta")
        assert resp.status_code == WorkspaceInvalid.http_status == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"

    def test_chapter_service_not_found(self, client: TestClient) -> None:
        resp = client.get("/_t/svc_chapter_not_found")
        assert resp.status_code == ChapterNotFound.http_status == 404
        assert resp.json()["error"]["code"] == "CHAPTER_NOT_FOUND"

    def test_chapter_service_card_invalid(self, client: TestClient) -> None:
        resp = client.get("/_t/svc_chapter_card")
        assert resp.status_code == ChapterCardInvalid.http_status == 422
        assert resp.json()["error"]["code"] == "CHAPTER_CARD_INVALID"

    def test_chapter_service_other_falls_into_invalid_state(
        self, client: TestClient
    ) -> None:
        resp = client.get("/_t/svc_chapter_other")
        assert resp.status_code == ChapterInvalidState.http_status == 409
        assert resp.json()["error"]["code"] == "CHAPTER_INVALID_STATE"

    def test_chapter_versioning_error_to_409(self, client: TestClient) -> None:
        resp = client.get("/_t/svc_versioning")
        assert resp.status_code == ChapterVersionConflict.http_status == 409
        assert resp.json()["error"]["code"] == "CHAPTER_VERSION_CONFLICT"

    def test_profile_not_found_error_to_422(self, client: TestClient) -> None:
        """P3-B-11:``ProfileNotFoundError`` → 422 ``CHAPTER_INVALID_PROFILE``。

        ChapterService.write/rewrite 入口 ``profile_override`` 引用未声明
        profile_id 时由 ``_ProfileOverrideRouter.__init__`` 校验抛,API 层
        翻译表(``_translate_service_error``)按 isinstance 翻译为
        ChapterInvalidProfile,前端可定位非法 profile_id。
        """
        resp = client.get("/_t/svc_profile_not_found")
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "CHAPTER_INVALID_PROFILE"
        assert "p_unknown" in body["error"]["message"]

    def test_provider_call_cap_translates_with_authoritative_counts(
        self,
    ) -> None:
        translated = _translate_service_error(
            ProviderCallCapExceeded(
                workspace_id="ws-r5",
                used=96,
                cap=96,
            )
        )
        assert isinstance(translated, ProviderCallCapReached)
        assert translated.http_status == 409
        assert translated.details == {
            "workspace_id": "ws-r5",
            "used": 96,
            "cap": 96,
        }

    def test_outline_length_calibration_gate_has_stable_contract(self) -> None:
        translated = _translate_service_error(
            OutlineLengthCalibrationRequired(
                workspace_id="ws-r9-v6",
                model="qwen3.8-max",
                genre="都市悬疑",
                reason="model_calibration_missing",
            )
        )
        assert translated is not None
        assert translated.code == "OUTLINE_LENGTH_CALIBRATION_REQUIRED"
        assert translated.http_status == 409
        assert translated.details == {
            "workspace_id": "ws-r9-v6",
            "model": "qwen3.8-max",
            "genre": "都市悬疑",
            "reason": "model_calibration_missing",
            "blocked_stage": "volume_refine",
            "provider_requests_started": 0,
        }

    @pytest.mark.parametrize(
        ("exc", "expected_type", "expected_status", "failure_layer"),
        [
            (
                ContinuityLedgerNotReadyError(
                    chapter_id="ch-0001", reason="missing validated ledger"
                ),
                ContinuityLedgerNotReady,
                409,
                "continuity_ledger_before_ready",
            ),
            (
                ForcePassContinuityLedgerNotReadyError(
                    chapter_id="ch-0001", reason="missing final"
                ),
                ContinuityLedgerNotReady,
                409,
                "force_pass_continuity_ledger_before_ready",
            ),
            (
                ChapterCardContinuityStaleError(
                    chapter_id="ch-0002",
                    reason="hash mismatch",
                    frozen=None,
                    current=None,
                ),
                ChapterCardContinuityStale,
                409,
                "writer_preflight_continuity_stale",
            ),
            (
                CardContinuityLedgerStaleError(
                    chapter_id="ch-0002",
                    reason="hash mismatch",
                    frozen=None,
                    current=None,
                ),
                ChapterCardContinuityStale,
                409,
                "card_approval_continuity_stale",
            ),
            (
                ChapterCardContinuityBridgeError(
                    chapter_id="ch-0002", reason="bridge missing"
                ),
                ChapterCardContinuityBridgeRequired,
                422,
                "writer_preflight_continuity_bridge",
            ),
            (
                CardContinuityBridgeRequiredError(
                    chapter_id="ch-0002", reason="bridge missing"
                ),
                ChapterCardContinuityBridgeRequired,
                422,
                "card_approval_continuity_bridge",
            ),
        ],
    )
    def test_r4_continuity_errors_are_stable_api_contracts(
        self,
        exc: Exception,
        expected_type: type,
        expected_status: int,
        failure_layer: str,
    ) -> None:
        translated = _translate_service_error(exc)
        assert isinstance(translated, expected_type)
        assert translated.http_status == expected_status
        assert translated.details["failure_layer"] == failure_layer


class TestOutlineBuilderErrorTranslation:
    """v2 W3.5 UI 缺口1:OutlineBuilderError → 结构化错误码(原落 INTERNAL_ERROR)。

    钉住:① 一致性硬伤(contradictions 非空)→ OUTLINE_CONSISTENCY_CONFLICT 422 +
    details 含 contradictions/evidence(前端列冲突 + 高亮实体名);② 通用 JSON/schema
    失败 → OUTLINE_BUILD_FAILED 422 + details 含 stage/model_used。
    """

    def test_consistency_conflict_translates_with_details(self) -> None:
        from src.core.agents.outline_builder import OutlineBuilderError

        exc = OutlineBuilderError(
            "跨段矛盾",
            stage="volume_plans",
            raw_output="顾崇山 vs 顾盛礼",
            model_used="outline_consistency_validator",
            contradictions=["顾崇山在 world 段被写成顾盛礼", "小陆/小纪混为一人"],
            coverage_gaps=[],
            evidence="world 段与 characters 段名字互斥",
        )
        translated = _translate_service_error(exc)
        assert translated is not None
        assert translated.code == "OUTLINE_CONSISTENCY_CONFLICT"
        assert translated.http_status == 422
        assert translated.details is not None
        assert translated.details["stage"] == "volume_plans"
        assert len(translated.details["contradictions"]) == 2
        assert "顾盛礼" in translated.details["contradictions"][0]
        assert translated.details["evidence"] == "world 段与 characters 段名字互斥"

    def test_generic_build_failure_translates(self) -> None:
        from src.core.agents.outline_builder import OutlineBuilderError

        exc = OutlineBuilderError(
            "mini_plan JSON 非法",
            stage="mini_plan",
            raw_output="{bad json",
            model_used="deepseek-v4-pro",
        )
        translated = _translate_service_error(exc)
        assert translated is not None
        assert translated.code == "OUTLINE_BUILD_FAILED"
        assert translated.http_status == 422
        assert translated.details == {
            "stage": "mini_plan",
            "model_used": "deepseek-v4-pro",
        }

    def test_arc_span_builder_failure_keeps_specific_code(self) -> None:
        from src.core.agents.outline_builder import OutlineBuilderError

        exc = OutlineBuilderError(
            "副本跨度过短",
            stage="volume_plans",
            raw_output="v1:v1-arc-2:span=2",
            model_used="outline_arc_span_gate",
            violations=[
                {
                    "rule": "arc_too_short",
                    "volume_id": "v1",
                    "arc_id": "v1-arc-2",
                    "arc_type": "副本",
                    "chapter_range": [3, 4],
                    "span_length": 2,
                }
            ],
        )
        translated = _translate_service_error(exc)
        assert translated is not None
        assert translated.code == "OUTLINE_ARC_TOO_SHORT"
        assert translated.http_status == 422
        assert translated.details["stage"] == "volume_plans"
        assert translated.details["violations"][0]["span_length"] == 2

    def test_mini_plan_setting_conflict_keeps_r3_code(self) -> None:
        from src.core.agents.outline_builder import OutlineBuilderError

        exc = OutlineBuilderError(
            "MINI_PLAN_SETTING_CONFLICT",
            stage="mini_plan",
            raw_output="",
            model_used="mock-setting-gate",
            violations=[{"constraint_id": "a" * 64}],
            error_code="MINI_PLAN_SETTING_CONFLICT",
            contract_hash="b" * 64,
            attempts=3,
        )
        translated = _translate_service_error(exc)
        assert translated is not None
        assert translated.code == "MINI_PLAN_SETTING_CONFLICT"
        assert translated.http_status == 422
        assert translated.details["contract_hash"] == "b" * 64
        assert translated.details["attempts"] == 3

    def test_plot_setting_conflict_keeps_r9_code(self) -> None:
        from src.core.agents.outline_builder import OutlineBuilderError

        exc = OutlineBuilderError(
            "PLOT_SETTING_CONFLICT",
            stage="plot",
            raw_output="",
            model_used="mock-setting-gate",
            violations=[{"constraint_id": "c" * 64}],
            error_code="PLOT_SETTING_CONFLICT",
            contract_hash="d" * 64,
            attempts=2,
        )
        translated = _translate_service_error(exc)
        assert translated is not None
        assert translated.code == "PLOT_SETTING_CONFLICT"
        assert translated.http_status == 422
        assert translated.details["failure_layer"] == "plot"
        assert translated.details["contract_hash"] == "d" * 64
        assert translated.details["attempts"] == 2

    def test_volume_plans_setting_conflict_keeps_r9_v2_code(self) -> None:
        from src.core.agents.outline_builder import OutlineBuilderError

        exc = OutlineBuilderError(
            "VOLUME_PLANS_SETTING_CONFLICT",
            stage="volume_plans",
            raw_output="",
            model_used="mock-setting-gate",
            violations=[{"constraint_id": "e" * 64}],
            error_code="VOLUME_PLANS_SETTING_CONFLICT",
            contract_hash="f" * 64,
            attempts=2,
        )
        translated = _translate_service_error(exc)
        assert translated is not None
        assert translated.code == "VOLUME_PLANS_SETTING_CONFLICT"
        assert translated.http_status == 422
        assert translated.details["failure_layer"] == "volume_plans"
        assert translated.details["contract_hash"] == "f" * 64
        assert translated.details["attempts"] == 2


class TestChapterCardSemanticErrorTranslationR3:
    @pytest.mark.parametrize(
        "code",
        [
            "CHAPTER_CARD_SETTING_CONFLICT",
            "CHAPTER_CARD_CONTRACT_CONFLICT",
        ],
    )
    def test_generation_conflicts_keep_specific_code(self, code: str) -> None:
        from src.core.agents.chapter_card_generator import (
            ChapterCardGeneratorError,
        )

        exc = ChapterCardGeneratorError(
            code,
            raw_output="",
            model_used="mock-card",
            error_code=code,
            details={"failure_layer": "chapter_card"},
        )
        translated = _translate_service_error(exc)
        assert translated is not None
        assert translated.code == code
        assert translated.http_status == 422

    def test_stale_hash_keeps_specific_code(self) -> None:
        from src.services.chapter_service import ChapterCardSettingStaleError

        exc = ChapterCardSettingStaleError(
            chapter_id="ch-0006",
            card_contract_hash="c" * 64,
            current_contract_hash="d" * 64,
        )
        translated = _translate_service_error(exc)
        assert translated is not None
        assert translated.code == "CHAPTER_CARD_SETTING_STALE"
        assert translated.http_status == 409
        assert translated.details["chapter_id"] == "ch-0006"
        assert translated.details["constraint_id"]


class TestMiddlewareRegistered:
    """额外保险:确认 create_app 把三个中间件都装上,且顺序正确。"""

    def test_middleware_stack_order(self) -> None:
        app = create_app()
        # starlette 的 user_middleware 每项是 Middleware(cls=..., options=...);
        # ``add_middleware`` 用 ``insert(0, ...)``,所以 list[0] 是最后注册的(最外层)。
        actual_classes = [m.cls for m in app.user_middleware]
        assert RequestIDMiddleware in actual_classes
        assert AccessLogMiddleware in actual_classes
        assert ApiErrorTranslationMiddleware in actual_classes
        # 期望从外到内:RequestID -> AccessLog -> Translation
        assert actual_classes.index(RequestIDMiddleware) < actual_classes.index(
            AccessLogMiddleware
        ) < actual_classes.index(ApiErrorTranslationMiddleware)


class TestRegisterIsIdempotent:
    """register_exception_handlers 重复调用不破坏 app(简单冒烟)。"""

    def test_double_register(self) -> None:
        app = create_app()
        register_exception_handlers(app)
        register_exception_handlers(app)
        client = TestClient(app)
        resp = client.get("/api/v1/system/health")
        assert resp.status_code == 200
