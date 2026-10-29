"""路由层(P3-B-05 引入)。

按 ``WriterProfilesConfig`` 把 agent 调用映射到具体 ``WriterProfile`` +
``LLMClient``;Writer agent 的规则匹配走 ``rule_eval`` 受限 AST 求值。

公共出口
========

- :class:`ModelRouter` —— 路由器主入口,接收 ``WriterProfilesConfig``,提供
  ``route(agent_name, ...)`` 返回 ``RoutedTarget``。
- :class:`RoutedTarget` —— ``(profile, client, complete_kwargs)`` 三元组,
  供 P3-B-06 改造后的 agent 直接 ``client.complete(messages, **complete_kwargs)``。
- :func:`evaluate_rule` —— 规则字符串受限 AST 求值(测试 / ConfigPage 预检
  规则字面也可调)。
- :class:`RuleEvalError` —— 规则求值异常(语法 / 白名单 / 字段名 / 结果类型)。
"""

from src.core.routing.router import ModelRouter, RoutedTarget
from src.core.routing.rule_eval import RuleEvalError, evaluate_rule

__all__ = [
    "ModelRouter",
    "RoutedTarget",
    "RuleEvalError",
    "evaluate_rule",
]
