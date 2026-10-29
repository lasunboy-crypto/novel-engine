"""路由规则字符串表达式受限 AST 求值(P3-B-05 引入)。

对应 P3-plan 决策 2 选 B:不用 ``eval(condition, restricted_globals)``
(注入风险高),也不用纯 if-else 列表(失去表达力);改用 ``ast.parse`` +
**白名单 AST 节点检查** + **白名单字段名检查**双重 sandbox。

支持的语法子集
==============

- 比较操作符:``==`` / ``!=`` / ``>`` / ``>=`` / ``<`` / ``<=`` /
  ``in`` / ``not in``
- 布尔组合:``and`` / ``or`` / ``not``
- 字面量:``True`` / ``False`` / ``None`` / 整数 / 浮点 / 字符串
- 列表 / 元组字面量(配合 ``x in [...]`` 用法)
- 标识符:仅允许出现在 ``context`` 字典 key 集合内的名字

不支持的能力(被静态检查拒绝,触发 ``RuleEvalError``)
======================================================

- 函数调用(``foo()``)、属性访问(``a.b``)、下标(``a[0]``)
- 算术运算(``+`` / ``-`` / ``*`` / ``/`` / ``%`` 等)
- 任何形式的赋值 / 推导 / lambda / import / yield / await
- 引用未在 context 中声明的字段名

设计动机
========

规则字符串来自 ``config.yaml`` / 工作区 ``project.yaml``,**配置文件来源
可控但仍是字符串输入**——若引擎默认 yaml 被未来某个工具误生成或工作区
yaml 被恶意拼接,纯 ``eval`` 路径会暴露 RCE 面。白名单 AST 在解析阶段
直接拒绝非法节点,守住 sandbox 边界,避免依赖运行时 globals 限制(后者
历史多次绕过)。
"""

from __future__ import annotations

import ast
from typing import Any


class RuleEvalError(ValueError):
    """规则求值失败:语法非法 / 节点不在白名单 / 引用未声明字段 / 结果非 bool。"""


# 白名单 AST 节点。`ast.walk` 遍历整棵语法树,任一节点不在本元组里都拒。
# 注意:``ast.Load`` 是变量读上下文标记,合法 ``Name`` / ``Constant`` 节点
# 都会带,必须放进白名单;但 ``ast.Store`` / ``ast.Del`` 不在白名单——本
# 表达式 DSL 不允许赋值 / 删除。
_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,   # 顶层包装节点
    ast.BoolOp,       # and / or
    ast.UnaryOp,      # not
    ast.Compare,      # ==, !=, <, <=, >, >=, in, not in
    ast.Name,         # 标识符(进一步走 context key 白名单)
    ast.Constant,     # 字面量(数字 / 字符串 / True / False / None)
    ast.Load,         # 读上下文标记
    ast.List,         # 列表字面量(配 ``x in [a, b]``)
    ast.Tuple,        # 元组字面量
    # 布尔操作符具体类型
    ast.And,
    ast.Or,
    ast.Not,
    # 比较操作符具体类型
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)


def evaluate_rule(condition: str, context: dict[str, Any]) -> bool:
    """在受限 AST 白名单下求值布尔表达式。

    :param condition: 规则字符串,如 ``"is_arc_finale == True"`` /
        ``"tension_level >= 9"`` / ``"arc_phase in ['climax', 'falling']"``。
    :param context: 求值上下文,通常是 ``ChapterCard.model_dump()``;表达式
        中的标识符必须在此字典 key 集合内,否则拒。
    :return: 求值结果(必须为 ``bool``,否则视为规则写错)。
    :raises RuleEvalError: 语法错 / 含非法 AST 节点 / 引用未声明字段 / 结果非 bool。
    """
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError as exc:
        raise RuleEvalError(
            f"规则语法错: {condition!r}; 详情: {exc.msg}"
        ) from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleEvalError(
                f"规则含非法 AST 节点 {type(node).__name__!r}; "
                f"表达式: {condition!r}; 仅允许:比较 / 布尔组合 / 字面量 / "
                "白名单字段标识符。"
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in context:
            raise RuleEvalError(
                f"规则引用未声明字段 {node.id!r}; 表达式: {condition!r}; "
                f"当前 context 可访问字段: {sorted(context.keys())}"
            )

    # ``__builtins__`` 设为空 dict 阻断对 ``len`` / ``open`` / ``__import__``
    # 等内建函数的访问(双保险——白名单已拒绝 ``ast.Call``,但严格起见再封
    # 一层 globals)。``context`` 作为 locals 提供字段名解析。
    compiled = compile(tree, "<routing-rule>", "eval")
    result = eval(compiled, {"__builtins__": {}}, context)  # noqa: S307
    if not isinstance(result, bool):
        raise RuleEvalError(
            f"规则求值结果应为 bool,实际 {type(result).__name__}={result!r}; "
            f"表达式: {condition!r}"
        )
    return result
