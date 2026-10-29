"""核心 Pydantic 模型包。

共享常量
========

- ``ID_PATTERN``:``workspace_id`` / ``chapter_id`` 等"会落盘作为目录或文件名"
  的标识字段的字符集与长度白名单(对齐 v2_wishlist "P2 收口前必修"第 1 条 +
  P2-B-05)。仅允许字母数字下划线连字符,长度 1-64。
  P1 单机本地无恶意输入向量,P2+ 多人/远程模式启动前必须修。
"""

ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"

# Workspace IDs normally keep the project-wide path-safe contract above.  The
# frozen POST-V8 ABC qwen3.8-27b arm is the one intentional exception: its
# exact, reserved product workspace ID contains the model's literal dot.  The
# selection activity service still refuses that reserved ID unless the exact
# product-ready activity root/config/data tuple is active, so this pattern does
# not grant ordinary production callers a dotted workspace namespace.
WORKSPACE_ID_PATTERN = (
    r"^(?:[a-zA-Z0-9_-]{1,64}|post-v8-abc-qwen3\.8-27b-selection-v1)$"
)
