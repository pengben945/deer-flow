"""Run status and disconnect mode enums."""

# (学习注释) RunStatus 状态机:
#   pending → running → success / error / timeout / interrupted
# "interrupted" 支持两种子行为（interrupt vs rollback），
# 由 manager.py cancel() 的 action 参数控制
# DisconnectMode: SSE 断开策略 — cancel(取消后台任务) | continue(继续后台运行)

from enum import StrEnum


class RunStatus(StrEnum):
    """Lifecycle status of a single run."""

    pending = "pending"
    running = "running"
    success = "success"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"


class DisconnectMode(StrEnum):
    """Behaviour when the SSE consumer disconnects."""

    cancel = "cancel"
    continue_ = "continue"
