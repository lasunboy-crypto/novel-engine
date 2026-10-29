"""workspace 日志上下文绑定防回退测(v2 间隙 A3②,2026-06-13)。

根因:``WorkspaceRoutingHandler`` 按 ``llm_request`` / ``llm_response`` 事件的
``workspace_id`` 字段分流日志文件,该字段来自 structlog contextvars;相关链路
此前丢失绑定 → 调用全落 ``system_logs/``(工作区账面每书少计 ~86 次调用,
质量闸成本审计 §7.2；主要涉及 outline、卡生成和 ready 后富记忆链):

1. **outline 链**:``outline_service`` 7 个 LLM 调用点零绑定 → 修复 = 各点
   ``with bind_workspace_context(workspace_id)``;
2. **卡生成链**:``chapter_card_service`` 2 个 agent 调用点零绑定 → 同上;
3. **ready 后富记忆线程**:裸 ``Thread`` 不继承 contextvars → memory extractor、
   character disambiguator 与 plot thread merger 的逐次 usage 只出现在 stdout，
   工作区 JSONL 缺行。修复 = daemon thread 提交点 ``copy_context().run``。

测法分两层:富记忆线程传播是**行为测**；service 绑定是 **AST 结构断言**
(LLM 调用必须落在 ``with bind_workspace_context(...)`` 块内 —— 对齐 w2-s2 债② "全链路接线
源码断言" 先例,防止后续重构把调用挪出 with 块而静默回归)。

谁可能破坏:任一重构 ``outline_service`` 或 ``chapter_card_service`` LLM 调用段，
或给 outline·卡链加新 LLM 调用点但忘绑定的 session(新增调用点不会被既有断言自动覆盖,
本文件 docstring 即提醒:新增点须同步加 (method, agent) 对)。
"""

from __future__ import annotations

import ast
import inspect
from threading import Event

import pytest
import structlog

import src.services.chapter_card_service as chapter_card_service_module
import src.services.chapter_service as chapter_service_module
import src.services.outline_service as outline_service_module
from src.api.logging import bind_workspace_context
from src.services.attempt_ledger import bind_attempt_scope, current_attempt_scope


class TestBindWorkspaceContextHelper:
    """helper 本体:进绑出清,嵌套退出只 unbind 自己的 key。"""

    def test_binds_inside_and_clears_after_exit(self) -> None:
        assert "workspace_id" not in structlog.contextvars.get_contextvars()
        with bind_workspace_context("ws-a3"):
            assert (
                structlog.contextvars.get_contextvars()["workspace_id"]
                == "ws-a3"
            )
        assert "workspace_id" not in structlog.contextvars.get_contextvars()

    def test_nested_exit_restores_outer_value(self) -> None:
        with bind_workspace_context("ws-outer"):
            with bind_workspace_context("ws-inner"):
                assert (
                    structlog.contextvars.get_contextvars()["workspace_id"]
                    == "ws-inner"
                )
            assert (
                structlog.contextvars.get_contextvars()["workspace_id"]
                == "ws-outer"
            )


class TestMemoryEnrichmentWorkerContextPropagation:
    """ready 后 daemon thread 必须继承日志与 Provider attempt 上下文。"""

    def test_background_task_inherits_chapter_and_attempt_context(self) -> None:
        captured: dict[str, str | None] = {}
        completed = Event()

        def task() -> None:
            context = structlog.contextvars.get_contextvars()
            scope = current_attempt_scope()
            captured.update(
                {
                    "workspace_id": context.get("workspace_id"),
                    "chapter_id": context.get("chapter_id"),
                    "trace_id": context.get("trace_id"),
                    "scope_workspace_id": (
                        scope.workspace_id if scope is not None else None
                    ),
                    "scope_run_id": scope.run_id if scope is not None else None,
                    "scope_task_id": scope.task_id if scope is not None else None,
                    "scope_chapter_id": (
                        scope.chapter_id if scope is not None else None
                    ),
                    "scope_provider_call_cap": (
                        str(scope.provider_call_cap)
                        if scope is not None
                        else None
                    ),
                }
            )
            completed.set()

        with bind_attempt_scope(
            workspace_id="ws-enrichment",
            run_id="run-enrichment",
            task_id="run-enrichment:ch-0001:pipeline-attempt-3",
            chapter_id="ch-0001",
            provider_call_cap=180,
        ):
            with structlog.contextvars.bound_contextvars(
                workspace_id="ws-enrichment",
                chapter_id="ch-0001",
                trace_id="ch-0001-write0",
            ):
                chapter_service_module.ChapterService._run_memory_enrichment_in_background(
                    task
                )

        assert completed.wait(timeout=2), "后台富记忆任务未完成"
        assert captured == {
            "workspace_id": "ws-enrichment",
            "chapter_id": "ch-0001",
            "trace_id": "ch-0001-write0",
            "scope_workspace_id": "ws-enrichment",
            "scope_run_id": "run-enrichment",
            "scope_task_id": "run-enrichment:ch-0001:pipeline-attempt-3",
            "scope_chapter_id": "ch-0001",
            "scope_provider_call_cap": "180",
        }
        source = inspect.getsource(
            chapter_service_module.ChapterService._run_memory_enrichment_in_background
        )
        assert "copy_context().run" in source


def _calls_inside_bind_workspace_with(
    module: object, method_qualname: str
) -> set[str]:
    """解析模块 AST,返回 ``method_qualname`` 内所有落在
    ``with bind_workspace_context(...)`` 块中的被调函数名集合。

    ``method_qualname`` 形如 ``"OutlineService.generate_outline"`` 或顶层
    函数名;只匹配函数名结尾段(Attribute 调用取属性名)。
    """
    source = inspect.getsource(module)  # type: ignore[arg-type]
    tree = ast.parse(source)
    cls_name, _, fn_name = method_qualname.rpartition(".")

    def find_function(node: ast.AST) -> ast.FunctionDef | None:
        for child in ast.walk(node):
            if isinstance(child, ast.ClassDef) and cls_name:
                if child.name != cls_name:
                    continue
                for item in child.body:
                    if (
                        isinstance(item, ast.FunctionDef)
                        and item.name == fn_name
                    ):
                        return item
            elif isinstance(child, ast.FunctionDef) and not cls_name:
                if child.name == fn_name:
                    return child
        return None

    fn = find_function(tree)
    assert fn is not None, f"未找到 {method_qualname}"

    inside: set[str] = []  # type: ignore[assignment]
    inside = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.With):
            continue
        is_bind_ws = any(
            isinstance(item.context_expr, ast.Call)
            and (
                (
                    isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == "bind_workspace_context"
                )
                or (
                    isinstance(item.context_expr.func, ast.Attribute)
                    and item.context_expr.func.attr
                    == "bind_workspace_context"
                )
            )
            for item in node.items
        )
        if not is_bind_ws:
            continue
        for inner in node.body:
            for sub in ast.walk(inner):
                if isinstance(sub, ast.Call):
                    if isinstance(sub.func, ast.Name):
                        inside.add(sub.func.id)
                    elif isinstance(sub.func, ast.Attribute):
                        inside.add(sub.func.attr)
    return inside


def _calls_in_method(module: object, method_qualname: str) -> set[str]:
    """返回指定方法内直接/嵌套出现的全部调用名，用于锁入口委托。"""

    source = inspect.getsource(module)  # type: ignore[arg-type]
    tree = ast.parse(source)
    cls_name, _, fn_name = method_qualname.rpartition(".")
    for child in ast.walk(tree):
        if not isinstance(child, ast.ClassDef) or child.name != cls_name:
            continue
        for item in child.body:
            if not isinstance(item, ast.FunctionDef) or item.name != fn_name:
                continue
            calls: set[str] = set()
            for sub in ast.walk(item):
                if not isinstance(sub, ast.Call):
                    continue
                if isinstance(sub.func, ast.Name):
                    calls.add(sub.func.id)
                elif isinstance(sub.func, ast.Attribute):
                    calls.add(sub.func.attr)
            return calls
    raise AssertionError(f"未找到 {method_qualname}")


class TestServiceBindingStructure:
    """service 层 10 处 LLM 调用点必须在 bind_workspace_context 块内(AST 断言)。"""

    @pytest.mark.parametrize(
        ("method", "agent_call"),
        [
            # Public dispatch now checks the authoring confirmation first; the
            # existing LLM orchestration and its context binding live here.
            ("OutlineService._generate_outline", "build_outline"),
            ("OutlineService.regenerate_section", "build_world"),
            ("OutlineService.regenerate_section", "build_characters"),
            ("OutlineService.regenerate_section", "build_plot"),
            ("OutlineService.regenerate_section", "build_volume_plans"),
            ("OutlineService.regenerate_section", "generate_mini_plan"),
            (
                "OutlineService._run_commit_gates",
                "validate_volume_seam_replay",
            ),
        ],
    )
    def test_outline_service_llm_calls_are_bound(
        self, method: str, agent_call: str
    ) -> None:
        inside = _calls_inside_bind_workspace_with(
            outline_service_module, method
        )
        assert agent_call in inside, (
            f"{method} 的 {agent_call}(...) 不在 bind_workspace_context 块内"
            "(A3② 回归:该调用的 llm_response 将落 system_logs)"
        )

    @pytest.mark.parametrize(
        "method",
        [
            "ChapterCardService.generate_single_chapter_card",
            "ChapterCardService.retry_references",
        ],
    )
    def test_chapter_card_entrypoints_delegate_to_bound_helper(
        self, method: str
    ) -> None:
        calls = _calls_in_method(chapter_card_service_module, method)
        assert "_generate_card_with_setting_conflict_retry" in calls, (
            f"{method} 未委托共享 Setting retry + workspace 绑定 helper"
        )

    def test_chapter_card_shared_llm_calls_are_bound(self) -> None:
        method = "ChapterCardService._generate_card_with_setting_conflict_retry"
        inside = _calls_inside_bind_workspace_with(
            chapter_card_service_module, method
        )
        assert "generate_chapter_card" in inside, (
            f"{method} 的 generate_chapter_card(...) 不在 bind_workspace_context 块内"
            "(A3② 回归:该调用的 llm_response 将落 system_logs)"
        )
