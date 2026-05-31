"""Runs endpoints — create, stream, wait, cancel.

Implements the LangGraph Platform runs API on top of
:class:`deerflow.agents.runs.RunManager` and
:class:`deerflow.agents.stream_bridge.StreamBridge`.

SSE format is aligned with the LangGraph Platform protocol so that
the ``useStream`` React hook from ``@langchain/langgraph-sdk/react``
works without modification.
"""

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║ 【Layer 1 - API 路由器】 Thread Runs 完整调用链学习注释                        ║
# ║                                                                              ║
# ║ 架构位置: Gateway API 最外层，FastAPI 路由定义                                ║
# ║ 职责: 定义 RESTful 端点，通过 @require_permission 做权限校验，                 ║
# ║        将实际业务委派给 Service 层 (services.py)                              ║
# ║                                                                              ║
# ║ 设计哲学: "瘦路由 + 胖服务" — 路由只做 HTTP 协议适配，不含业务逻辑             ║
# ║ SSE 格式对齐 LangGraph Platform 协议，useStream React Hook 零修改可工作       ║
# ║                                                                              ║
# ║ 调用链入口: FastAPI → 端点函数 → services.start_run() → runtime.run_agent()   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.gateway.authz import require_permission
from app.gateway.deps import get_checkpointer, get_current_user, get_feedback_repo, get_run_event_store, get_run_manager, get_run_store, get_stream_bridge
from app.gateway.services import sse_consumer, start_run
from deerflow.runtime import RunRecord, serialize_channel_values

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/threads", tags=["runs"])


# ==============================================================================
# (学习注释) RunCreateRequest —— 核心请求模型
# 覆盖了 LangGraph Platform 标准参数 + DeerFlow 扩展（context 字段）
# 面试重点: context 字段是 DeerFlow 的"后门"——携带 model_name、thinking_enabled、
# agent_name 等运行时配置，由 services.py 的 merge_run_context_overrides() 注入
# ==============================================================================


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RunCreateRequest(BaseModel):
    assistant_id: str | None = Field(default=None, description="Agent / assistant to use")
    input: dict[str, Any] | None = Field(default=None, description="Graph input (e.g. {messages: [...]})")
    command: dict[str, Any] | None = Field(default=None, description="LangGraph Command")
    metadata: dict[str, Any] | None = Field(default=None, description="Run metadata")
    config: dict[str, Any] | None = Field(default=None, description="RunnableConfig overrides")
    context: dict[str, Any] | None = Field(default=None, description="DeerFlow context overrides (model_name, thinking_enabled, etc.)")
    webhook: str | None = Field(default=None, description="Completion callback URL")
    checkpoint_id: str | None = Field(default=None, description="Resume from checkpoint")
    checkpoint: dict[str, Any] | None = Field(default=None, description="Full checkpoint object")
    interrupt_before: list[str] | Literal["*"] | None = Field(default=None, description="Nodes to interrupt before")
    interrupt_after: list[str] | Literal["*"] | None = Field(default=None, description="Nodes to interrupt after")
    stream_mode: list[str] | str | None = Field(default=None, description="Stream mode(s)")
    stream_subgraphs: bool = Field(default=False, description="Include subgraph events")
    stream_resumable: bool | None = Field(default=None, description="SSE resumable mode")
    on_disconnect: Literal["cancel", "continue"] = Field(default="cancel", description="Behaviour on SSE disconnect")
    on_completion: Literal["delete", "keep"] = Field(default="keep", description="Delete temp thread on completion")
    multitask_strategy: Literal["reject", "rollback", "interrupt", "enqueue"] = Field(default="reject", description="Concurrency strategy")
    after_seconds: float | None = Field(default=None, description="Delayed execution")
    if_not_exists: Literal["reject", "create"] = Field(default="create", description="Thread creation policy")
    feedback_keys: list[str] | None = Field(default=None, description="LangSmith feedback keys")


# ------------------------------------------------------------------------------
# (学习注释) RunCreateRequest 关键字段解读:
#   - assistant_id: 选择 agent；非 "lead_agent" 时会创建自定义 agent
#   - input: {messages: [...]} 格式的 LangGraph 图输入
#   - context: DeerFlow 独有扩展，携带 model_name、thinking_enabled、agent_name 等
#   - multitask_strategy: 同一 thread 上多 Run 的并发策略 (reject/interrupt/rollback/enqueue)
#   - on_disconnect: SSE 断开时取消 (cancel) 还是后台继续 (continue)
#   - interrupt_before/after: 在哪些 LangGraph node 之前/之后中断（可控性中断）
# ------------------------------------------------------------------------------


class RunResponse(BaseModel):
    run_id: str
    thread_id: str
    assistant_id: str | None = None
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    multitask_strategy: str = "reject"
    created_at: str = ""
    updated_at: str = ""


# ==============================================================================
# (学习注释) 以下 12 个端点按功能分为 5 组:
#   创建类: create_run, stream_run, wait_run        → POST，创建 + 不同响应模式
#   查询类: list_runs, get_run                     → GET，查询运行记录
#   控制类: cancel_run                             → 取消/中断，支持 interrupt 和 rollback
#   流式类: join_run, stream_existing_run          → SSE 事件流订阅
#   数据类: list_thread_messages, list_run_messages, list_run_events, thread_token_usage
#
# 每个端点通过 @require_permission 保护，实际业务由 services.py 处理
# ==============================================================================


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_to_response(record: RunRecord) -> RunResponse:
    return RunResponse(
        run_id=record.run_id,
        thread_id=record.thread_id,
        assistant_id=record.assistant_id,
        status=record.status.value,
        metadata=record.metadata,
        kwargs=record.kwargs,
        multitask_strategy=record.multitask_strategy,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


# (学习注释) create_run → POST /{thread_id}/runs
# 最简单的端点：创建后台 Run 并立即返回，不等待执行完成。
# 调用链: create_run → services.start_run() → RunManager.create_or_reject()
#                                             → asyncio.create_task(run_agent())
@router.post("/{thread_id}/runs", response_model=RunResponse)
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def create_run(thread_id: str, body: RunCreateRequest, request: Request) -> RunResponse:
    """Create a background run (returns immediately)."""
    record = await start_run(body, thread_id, request)
    return _record_to_response(record)


# (学习注释) stream_run → POST /{thread_id}/runs/stream
# 【最核心端点】创建 Run 并通过 SSE 流式传输事件
# 返回 StreamingResponse，通过 Content-Location header 暴露 run 资源路径
# 调用链: stream_run → services.start_run() [创建+启动后台task]
#                     → services.sse_consumer() [SSE 生成器]
# 数据流: Worker → bridge.publish() → MemoryStreamBridge → sse_consumer.subscribe() → SSE frames
# SSE 事件类型: metadata | values | messages | error | end | :heartbeat
@router.post("/{thread_id}/runs/stream")
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def stream_run(thread_id: str, body: RunCreateRequest, request: Request) -> StreamingResponse:
    """Create a run and stream events via SSE.

    The response includes a ``Content-Location`` header with the run's
    resource URL, matching the LangGraph Platform protocol.  The
    ``useStream`` React hook uses this to extract run metadata.
    """
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    record = await start_run(body, thread_id, request)

    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            # LangGraph Platform includes run metadata in this header.
            # The SDK uses a greedy regex to extract the run id from this path,
            # so it must point at the canonical run resource without extra suffixes.
            "Content-Location": f"/api/threads/{thread_id}/runs/{record.run_id}",
        },
    )


# (学习注释) wait_run → POST /{thread_id}/runs/wait
# 创建 Run 并阻塞等待完成，返回最终状态字典
# 与 stream_run 的不同: 等待后台 task 完成后从 checkpointer 读最终 checkpoint
# 适合不需要实时流式的场景（如 Slack/Telegram IM 通道）
@router.post("/{thread_id}/runs/wait", response_model=dict)
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def wait_run(thread_id: str, body: RunCreateRequest, request: Request) -> dict:
    """Create a run and block until it completes, returning the final state."""
    record = await start_run(body, thread_id, request)

    if record.task is not None:
        try:
            await record.task
        except asyncio.CancelledError:
            pass

    checkpointer = get_checkpointer(request)
    config = {"configurable": {"thread_id": thread_id}}
    try:
        checkpoint_tuple = await checkpointer.aget_tuple(config)
        if checkpoint_tuple is not None:
            checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
            channel_values = checkpoint.get("channel_values", {})
            return serialize_channel_values(channel_values)
    except Exception:
        logger.exception("Failed to fetch final state for run %s", record.run_id)

    return {"status": record.status.value, "error": record.error}


# (学习注释) list_runs → GET /{thread_id}/runs
# 查询类端点，从 RunManager 获取 Run 列表
@router.get("/{thread_id}/runs", response_model=list[RunResponse])
@require_permission("runs", "read", owner_check=True)
async def list_runs(thread_id: str, request: Request) -> list[RunResponse]:
    """List all runs for a thread."""
    run_mgr = get_run_manager(request)
    records = await run_mgr.list_by_thread(thread_id)
    return [_record_to_response(r) for r in records]


@router.get("/{thread_id}/runs/{run_id}", response_model=RunResponse)
@require_permission("runs", "read", owner_check=True)
async def get_run(thread_id: str, run_id: str, request: Request) -> RunResponse:
    """Get details of a specific run."""
    run_mgr = get_run_manager(request)
    record = run_mgr.get(run_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _record_to_response(record)


# (学习注释) cancel_run → POST /{thread_id}/runs/{run_id}/cancel
# 控制类端点，中断一个正在运行的 Run
# 两个行为: interrupt(保留checkpoint,可resume) vs rollback(恢复到运行前状态)
# 通过 abort_event(asyncio.Event) + task.cancel() 双重机制保证一定能取消
@router.post("/{thread_id}/runs/{run_id}/cancel")
@require_permission("runs", "cancel", owner_check=True, require_existing=True)
async def cancel_run(
    thread_id: str,
    run_id: str,
    request: Request,
    wait: bool = Query(default=False, description="Block until run completes after cancel"),
    action: Literal["interrupt", "rollback"] = Query(default="interrupt", description="Cancel action"),
) -> Response:
    """Cancel a running or pending run.

    - action=interrupt: Stop execution, keep current checkpoint (can be resumed)
    - action=rollback: Stop execution, revert to pre-run checkpoint state
    - wait=true: Block until the run fully stops, return 204
    - wait=false: Return immediately with 202
    """
    run_mgr = get_run_manager(request)
    record = run_mgr.get(run_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    cancelled = await run_mgr.cancel(run_id, action=action)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is not cancellable (status: {record.status.value})",
        )

    if wait and record.task is not None:
        try:
            await record.task
        except asyncio.CancelledError:
            pass
        return Response(status_code=204)

    return Response(status_code=202)


# (学习注释) join_run → GET /{thread_id}/runs/{run_id}/join
# 流式类端点: 加入一个已有 Run 的 SSE 流，不创建新 Run
# 用于客户端重连/延迟加入场景，从 StreamBridge 订阅剩余事件
@router.get("/{thread_id}/runs/{run_id}/join")
@require_permission("runs", "read", owner_check=True)
async def join_run(thread_id: str, run_id: str, request: Request) -> StreamingResponse:
    """Join an existing run's SSE stream."""
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    record = run_mgr.get(run_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# (学习注释) stream_existing_run → GET/POST /{thread_id}/runs/{run_id}/stream
# /join 的功能超集: 多了"取消后继续流式"的能力
# LangGraph SDK 的 joinStream() 和 useStream 停止按钮都用 POST 到此端点
# 当 action=interrupt/rollback 时先取消 Run，再流式输出剩余缓冲事件
# 让客户端观察到干净的关闭
@router.api_route("/{thread_id}/runs/{run_id}/stream", methods=["GET", "POST"], response_model=None)
@require_permission("runs", "read", owner_check=True)
async def stream_existing_run(
    thread_id: str,
    run_id: str,
    request: Request,
    action: Literal["interrupt", "rollback"] | None = Query(default=None, description="Cancel action"),
    wait: int = Query(default=0, description="Block until cancelled (1) or return immediately (0)"),
):
    """Join an existing run's SSE stream (GET), or cancel-then-stream (POST).

    The LangGraph SDK's ``joinStream`` and ``useStream`` stop button both use
    ``POST`` to this endpoint.  When ``action=interrupt`` or ``action=rollback``
    is present the run is cancelled first; the response then streams any
    remaining buffered events so the client observes a clean shutdown.
    """
    run_mgr = get_run_manager(request)
    record = run_mgr.get(run_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    # Cancel if an action was requested (stop-button / interrupt flow)
    if action is not None:
        cancelled = await run_mgr.cancel(run_id, action=action)
        if cancelled and wait and record.task is not None:
            try:
                await record.task
            except (asyncio.CancelledError, Exception):
                pass
            return Response(status_code=204)

    bridge = get_stream_bridge(request)
    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==============================================================================
# (学习注释) 消息/事件/Token 用量端点
# 这些端点查询持久化存储(RunEventStore + FeedbackRepository)，不涉及运行时
# 区别于前面的创建/流式端点，它们是纯查询类端点
# ==============================================================================


# ---------------------------------------------------------------------------
# Messages / Events / Token usage endpoints
# ---------------------------------------------------------------------------


# (学习注释) list_thread_messages → GET /{thread_id}/messages
# 前端聊天界面的核心数据源:
# 1. 从 RunEventStore 获取消息（跨所有 Run）
# 2. 从 FeedbackRepository 获取用户反馈
# 3. 将反馈挂载到每个 Run 的最后一条 AI 消息上
# 支持 before_seq / after_seq 双向游标分页
@router.get("/{thread_id}/messages")
@require_permission("runs", "read", owner_check=True)
async def list_thread_messages(
    thread_id: str,
    request: Request,
    limit: int = Query(default=50, le=200),
    before_seq: int | None = Query(default=None),
    after_seq: int | None = Query(default=None),
) -> list[dict]:
    """Return displayable messages for a thread (across all runs), with feedback attached."""
    event_store = get_run_event_store(request)
    messages = await event_store.list_messages(thread_id, limit=limit, before_seq=before_seq, after_seq=after_seq)

    # Attach feedback to the last AI message of each run
    feedback_repo = get_feedback_repo(request)
    user_id = await get_current_user(request)
    feedback_map = await feedback_repo.list_by_thread_grouped(thread_id, user_id=user_id)

    # Find the last ai_message per run_id
    last_ai_per_run: dict[str, int] = {}  # run_id -> index in messages list
    for i, msg in enumerate(messages):
        if msg.get("event_type") == "ai_message":
            last_ai_per_run[msg["run_id"]] = i

    # Attach feedback field
    last_ai_indices = set(last_ai_per_run.values())
    for i, msg in enumerate(messages):
        if i in last_ai_indices:
            run_id = msg["run_id"]
            fb = feedback_map.get(run_id)
            msg["feedback"] = (
                {
                    "feedback_id": fb["feedback_id"],
                    "rating": fb["rating"],
                    "comment": fb.get("comment"),
                }
                if fb
                else None
            )
        else:
            msg["feedback"] = None

    return messages


# (学习注释) list_run_messages → GET /{thread_id}/runs/{run_id}/messages
# 单 Run 分页消息，响应 {data, has_more} 格式
# 通过请求 limit+1 条来判断是否有更多数据
@router.get("/{thread_id}/runs/{run_id}/messages")
@require_permission("runs", "read", owner_check=True)
async def list_run_messages(
    thread_id: str,
    run_id: str,
    request: Request,
    limit: int = Query(default=50, le=200, ge=1),
    before_seq: int | None = Query(default=None),
    after_seq: int | None = Query(default=None),
) -> dict:
    """Return paginated messages for a specific run.

    Response: { data: [...], has_more: bool }
    """
    event_store = get_run_event_store(request)
    rows = await event_store.list_messages_by_run(
        thread_id,
        run_id,
        limit=limit + 1,
        before_seq=before_seq,
        after_seq=after_seq,
    )
    has_more = len(rows) > limit
    data = rows[:limit] if has_more else rows
    return {"data": data, "has_more": has_more}


# (学习注释) list_run_events → GET /{thread_id}/runs/{run_id}/events
# 全事件流: 与 /messages 不同，返回所有 category 的事件
# (message + trace + error + middleware)，用于开发者工具中的"运行日志"面板
@router.get("/{thread_id}/runs/{run_id}/events")
@require_permission("runs", "read", owner_check=True)
async def list_run_events(
    thread_id: str,
    run_id: str,
    request: Request,
    event_types: str | None = Query(default=None),
    limit: int = Query(default=500, le=2000),
) -> list[dict]:
    """Return the full event stream for a run (debug/audit)."""
    event_store = get_run_event_store(request)
    types = event_types.split(",") if event_types else None
    return await event_store.list_events(thread_id, run_id, event_types=types, limit=limit)


# (学习注释) thread_token_usage → GET /{thread_id}/token-usage
# Token 用量聚合: 从 RunStore 查询该 thread 所有 Run 的 token 累计数据
# 返回 total_input_tokens / total_output_tokens / total_tokens / llm_call_count
@router.get("/{thread_id}/token-usage")
@require_permission("threads", "read", owner_check=True)
async def thread_token_usage(thread_id: str, request: Request) -> dict:
    """Thread-level token usage aggregation."""
    run_store = get_run_store(request)
    agg = await run_store.aggregate_tokens_by_thread(thread_id)
    return {"thread_id": thread_id, **agg}
