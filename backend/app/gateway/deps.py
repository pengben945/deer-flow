"""Centralized accessors for singleton objects stored on ``app.state``.

**Getters** (used by routers): raise 503 when a required dependency is
missing, except ``get_store`` which returns ``None``.

Initialization is handled directly in ``app.py`` via :class:`AsyncExitStack`.
"""

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║ 【依赖注入层】app.state 运行时单例管理                                          ║
# ║ langgraph_runtime(): AsyncExitStack 有序初始化所有单例                          ║
# ║ 初始化顺序: StreamBridge → 持久化引擎 → Checkpointer → Store →                 ║
# ║             RunRepository → RunEventStore → RunManager                         ║
# ║ get_* 函数族: 路由器通过 request.app.state 获取依赖，缺失返回 503               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, TypeVar, cast

from fastapi import FastAPI, HTTPException, Request
from langgraph.types import Checkpointer

from deerflow.config.app_config import AppConfig
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.runtime import RunContext, RunManager, StreamBridge
from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.runs.store.base import RunStore

if TYPE_CHECKING:
    from app.gateway.auth.local_provider import LocalAuthProvider
    from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
    from deerflow.persistence.thread_meta.base import ThreadMetaStore


T = TypeVar("T")


def get_config(request: Request) -> AppConfig:
    """Return the app-scoped ``AppConfig`` stored on ``app.state``."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="Configuration not available")
    return config


@asynccontextmanager
async def langgraph_runtime(app: FastAPI) -> AsyncGenerator[None, None]:
    """Bootstrap and tear down all LangGraph runtime singletons.

    Usage in ``app.py``::

        async with langgraph_runtime(app):
            yield
    """
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine_from_config
    from deerflow.runtime import make_store, make_stream_bridge
    from deerflow.runtime.checkpointer.async_provider import make_checkpointer
    from deerflow.runtime.events.store import make_run_event_store

    async with AsyncExitStack() as stack:
        config = getattr(app.state, "config", None)
        if config is None:
            raise RuntimeError("langgraph_runtime() requires app.state.config to be initialized")

        # 【1】StreamBridge: 事件管道（Worker → SSE Consumer），最先初始化
        app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))

        # Initialize persistence engine BEFORE checkpointer so that
        # auto-create-database logic runs first (postgres backend).
        # 【2】持久化引擎：先初始化数据库（PostgreSQL 自动建库）
        await init_engine_from_config(config.database)

        # 【3】Checkpointer: 状态检查点
        app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))
        app.state.store = await stack.enter_async_context(make_store(config))

        # 【5】初始化仓库层 — 共享 session_factory，减少数据库连接
        # Initialize repositories — one get_session_factory() call for all.
        sf = get_session_factory()
        if sf is not None:
            from deerflow.persistence.feedback import FeedbackRepository
            from deerflow.persistence.run import RunRepository

            app.state.run_store = RunRepository(sf)
            app.state.feedback_repo = FeedbackRepository(sf)
        else:
            from deerflow.runtime.runs.store.memory import MemoryRunStore

            app.state.run_store = MemoryRunStore()
            app.state.feedback_repo = None

        from deerflow.persistence.thread_meta import make_thread_store

        app.state.thread_store = make_thread_store(sf, app.state.store)

        # 【6】RunEventStore: 事件流存储（config 驱动: DB / JSONL / Memory）
        # Run event store (has its own factory with config-driven backend selection)
        run_events_config = getattr(config, "run_events", None)
        app.state.run_event_store = make_run_event_store(run_events_config)

        # 【7】RunManager: 运行注册表（内存 + 可选 RunStore 持久化）
        # RunManager with store backing for persistence
        app.state.run_manager = RunManager(store=app.state.run_store)

        try:
            yield
        finally:
            await close_engine()


# ==============================================================================
# (学习注释) getter 函数族 — 路由器通过 request → app.state.xxx 获取单例
# _require() 工厂 → 自动生成类型安全的 getter 闭包，缺失返回 503
# ==============================================================================


# ---------------------------------------------------------------------------
# Getters – called by routers per-request
# ---------------------------------------------------------------------------


def _require(attr: str, label: str) -> Callable[[Request], T]:
    """Create a FastAPI dependency that returns ``app.state.<attr>`` or 503."""

    def dep(request: Request) -> T:
        val = getattr(request.app.state, attr, None)
        if val is None:
            raise HTTPException(status_code=503, detail=f"{label} not available")
        return cast(T, val)

    dep.__name__ = dep.__qualname__ = f"get_{attr}"
    return dep


# (学习注释) get_stream_bridge: 获取 StreamBridge 单例
# 返回 app.state.stream_bridge — 事件管道（Worker → SSE Consumer）
# 路由器通过它订阅 SSE 事件流
get_stream_bridge: Callable[[Request], StreamBridge] = _require("stream_bridge", "Stream bridge")

# (学习注释) get_run_manager: 获取 RunManager 单例
# 返回 app.state.run_manager — 运行注册表（创建/查询/取消 Run）
# 路由器通过它创建 RunRecord、查询运行状态、中断执行
get_run_manager: Callable[[Request], RunManager] = _require("run_manager", "Run manager")

# (学习注释) get_checkpointer: 获取 Checkpointer 单例
# 返回 app.state.checkpointer — 状态检查点（持久化 agent 运行状态）
# wait_run 端点用它读取最终 checkpoint；worker 用它做状态持久化和 rollback
get_checkpointer: Callable[[Request], Checkpointer] = _require("checkpointer", "Checkpointer")

# (学习注释) get_run_event_store: 获取 RunEventStore 单例
# 返回 app.state.run_event_store — 事件流存储（消息/追踪/错误事件）
# 消息列表/事件列表端点通过它查询历史数据
get_run_event_store: Callable[[Request], RunEventStore] = _require("run_event_store", "Run event store")

# (学习注释) get_feedback_repo: 获取 FeedbackRepository 单例
# 返回 app.state.feedback_repo — 用户反馈存储（点赞/点踩/评论）
# list_thread_messages 端点用它附着反馈到消息上
get_feedback_repo: Callable[[Request], FeedbackRepository] = _require("feedback_repo", "Feedback")

# (学习注释) get_run_store: 获取 RunStore 单例
# 返回 app.state.run_store — Run 元数据持久化存储
# token_usage 端点用它做 Token 用量聚合查询
get_run_store: Callable[[Request], RunStore] = _require("run_store", "Run store")


def get_store(request: Request):
    """Return the global store (may be ``None`` if not configured)."""
    return getattr(request.app.state, "store", None)


def get_thread_store(request: Request) -> ThreadMetaStore:
    """Return the thread metadata store (SQL or memory-backed)."""
    val = getattr(request.app.state, "thread_store", None)
    if val is None:
        raise HTTPException(status_code=503, detail="Thread metadata store not available")
    return val


# (学习注释) get_run_context — 基础设施依赖聚合
# 将 checkpointer / store / event_store / thread_store 打包为 RunContext
# 传递给 worker.py 的 run_agent()，避免参数列表增长
def get_run_context(request: Request) -> RunContext:
    """Build a :class:`RunContext` from ``app.state`` singletons.

    Returns a *base* context with infrastructure dependencies.
    """
    config = get_config(request)
    return RunContext(
        checkpointer=get_checkpointer(request),
        store=get_store(request),
        event_store=get_run_event_store(request),
        run_events_config=getattr(config, "run_events", None),
        thread_store=get_thread_store(request),
        app_config=config,
    )


# ---------------------------------------------------------------------------
# Auth helpers (used by authz.py and auth middleware)
# ---------------------------------------------------------------------------

# Cached singletons to avoid repeated instantiation per request
_cached_local_provider: LocalAuthProvider | None = None
_cached_repo: SQLiteUserRepository | None = None


def get_local_provider() -> LocalAuthProvider:
    """Get or create the cached LocalAuthProvider singleton.

    Must be called after ``init_engine_from_config()`` — the shared
    session factory is required to construct the user repository.
    """
    global _cached_local_provider, _cached_repo
    if _cached_repo is None:
        from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
        from deerflow.persistence.engine import get_session_factory

        sf = get_session_factory()
        if sf is None:
            raise RuntimeError("get_local_provider() called before init_engine_from_config(); cannot access users table")
        _cached_repo = SQLiteUserRepository(sf)
    if _cached_local_provider is None:
        from app.gateway.auth.local_provider import LocalAuthProvider

        _cached_local_provider = LocalAuthProvider(repository=_cached_repo)
    return _cached_local_provider


async def get_current_user_from_request(request: Request):
    """Get the current authenticated user from the request cookie.

    Raises HTTPException 401 if not authenticated.
    """
    from app.gateway.auth import decode_token
    from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse, TokenError, token_error_to_code

    access_token = request.cookies.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.NOT_AUTHENTICATED, message="Not authenticated").model_dump(),
        )

    payload = decode_token(access_token)
    if isinstance(payload, TokenError):
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=token_error_to_code(payload), message=f"Token error: {payload.value}").model_dump(),
        )

    provider = get_local_provider()
    user = await provider.get_user(payload.sub)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.USER_NOT_FOUND, message="User not found").model_dump(),
        )

    # Token version mismatch → password was changed, token is stale
    if user.token_version != payload.ver:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.TOKEN_INVALID, message="Token revoked (password changed)").model_dump(),
        )

    return user


async def get_optional_user_from_request(request: Request):
    """Get optional authenticated user from request.

    Returns None if not authenticated.
    """
    try:
        return await get_current_user_from_request(request)
    except HTTPException:
        return None


async def get_current_user(request: Request) -> str | None:
    """Extract user_id from request cookie, or None if not authenticated.

    Thin adapter that returns the string id for callers that only need
    identification (e.g., ``feedback.py``). Full-user callers should use
    ``get_current_user_from_request`` or ``get_optional_user_from_request``.
    """
    user = await get_optional_user_from_request(request)
    return str(user.id) if user else None
