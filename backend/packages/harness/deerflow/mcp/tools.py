"""Load MCP tools using langchain-mcp-adapters."""

import asyncio
import atexit
import concurrent.futures
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool

from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.mcp.client import build_servers_config
from deerflow.mcp.oauth import build_oauth_tool_interceptor, get_initial_oauth_headers
from deerflow.reflection import resolve_variable

logger = logging.getLogger(__name__)

# Global thread pool for sync tool invocation in async environments
_SYNC_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="mcp-sync-tool")

# Register shutdown hook for the global executor
atexit.register(lambda: _SYNC_TOOL_EXECUTOR.shutdown(wait=False))


def _make_sync_tool_wrapper(coro: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """Build a synchronous wrapper for an asynchronous tool coroutine.

    Args:
        coro: The tool's asynchronous coroutine.
        tool_name: Name of the tool (for logging).

    Returns:
        A synchronous function that correctly handles nested event loops.

    为什么：MCP 工具是 async 协程，但 DeerFlow 的 subagent 执行器
    在线程池中同步调用工具（``SubagentExecutor.execute()`` → ``asyncio.run()``）。
    如果已经处于一个运行中的事件循环内（例如 LangGraph 流式处理期间），
    再次调用 ``asyncio.run()`` 会抛出 ``RuntimeError: asyncio.run()
    cannot be called from a running event loop``。下面的三路分支处理了这种情况：

      1. 已有运行中的事件循环 → 卸载到专用线程池
      2. 无运行中的事件循环 → 标准 ``asyncio.run()``
      3. 两者都失败 → 记录日志后传播异常

    边界案例：``_SYNC_TOOL_EXECUTOR`` 是进程级单例；如果进程正在关闭，
    执行器线程上的 ``asyncio.run`` 可能因事件循环已关闭而失败。
    ``atexit`` 注册的清理是尽力而为的。
    """

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        try:
            if loop is not None and loop.is_running():
                # 使用全局执行器避免嵌套事件循环问题，同时提升性能
                future = _SYNC_TOOL_EXECUTOR.submit(asyncio.run, coro(*args, **kwargs))
                return future.result()
            else:
                return asyncio.run(coro(*args, **kwargs))
        except Exception as e:
            logger.error(f"Error invoking MCP tool '{tool_name}' via sync wrapper: {e}", exc_info=True)
            raise

    return sync_wrapper


async def get_mcp_tools() -> list[BaseTool]:
    """Get all tools from enabled MCP servers.

    Returns:
        List of LangChain tools from all enabled MCP servers.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("langchain-mcp-adapters not installed. Install it to enable MCP tools: pip install langchain-mcp-adapters")
        return []

    # NOTE: We use ExtensionsConfig.from_file() instead of get_extensions_config()
    # to always read the latest configuration from disk. This ensures that changes
    # made through the Gateway API (which runs in a separate process) are immediately
    # reflected when initializing MCP tools.
    extensions_config = ExtensionsConfig.from_file()
    servers_config = build_servers_config(extensions_config)

    if not servers_config:
        logger.info("No enabled MCP servers configured")
        return []

    try:
        # Create the multi-server MCP client
        logger.info(f"Initializing MCP client with {len(servers_config)} server(s)")

        # Inject initial OAuth headers for server connections (tool discovery/session init)
        # 为什么：SSE/HTTP 传输在连接时进行身份认证。我们在将服务器配置
        # 传递给 MultiServerMCPClient 之前注入 Authorization header，
        # 这样客户端从一开始就建立经过认证的连接。后续的令牌刷新通过
        # OAuth 工具拦截器（interceptor）完成。
        initial_oauth_headers = await get_initial_oauth_headers(extensions_config)
        for server_name, auth_header in initial_oauth_headers.items():
            if server_name not in servers_config:
                continue
            if servers_config[server_name].get("transport") in ("sse", "http"):
                existing_headers = dict(servers_config[server_name].get("headers", {}))
                existing_headers["Authorization"] = auth_header
                servers_config[server_name]["headers"] = existing_headers

        tool_interceptors = []
        oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
        if oauth_interceptor is not None:
            tool_interceptors.append(oauth_interceptor)

        # Load custom interceptors declared in extensions_config.json
        # Format: "mcpInterceptors": ["pkg.module:builder_func", ...]
        # 工作方式：每个拦截器路径通过 resolve_variable 解析为一个
        # builder 可调用对象，然后被调用。生成的拦截器签名是
        # callable(tool_name, args, kwargs) → (args, kwargs) 或抛出异常 ——
        # 可用于认证头刷新、限流或审计日志。
        # 边界案例：builder 返回 None 时静默跳过（不注册拦截器）；
        # 返回非可调用的非 None 值时记录警告日志。
        raw_interceptor_paths = (extensions_config.model_extra or {}).get("mcpInterceptors")
        if isinstance(raw_interceptor_paths, str):
            raw_interceptor_paths = [raw_interceptor_paths]
        elif not isinstance(raw_interceptor_paths, list):
            if raw_interceptor_paths is not None:
                logger.warning(f"mcpInterceptors must be a list of strings, got {type(raw_interceptor_paths).__name__}; skipping")
            raw_interceptor_paths = []
        for interceptor_path in raw_interceptor_paths:
            try:
                builder = resolve_variable(interceptor_path)
                interceptor = builder()
                if callable(interceptor):
                    tool_interceptors.append(interceptor)
                    logger.info(f"Loaded MCP interceptor: {interceptor_path}")
                elif interceptor is not None:
                    logger.warning(f"Builder {interceptor_path} returned non-callable {type(interceptor).__name__}; skipping")
            except Exception as e:
                logger.warning(f"Failed to load MCP interceptor {interceptor_path}: {e}", exc_info=True)

        # 为什么 tool_name_prefix=True：多个 MCP 服务器可能暴露同名的工具
        # （例如两个服务器都定义了 "read_file"）。前缀将其消歧为
        # "<server_name>_<tool_name>"，防止 LLM 的 tool schema 中出现命名冲突。
        client = MultiServerMCPClient(servers_config, tool_interceptors=tool_interceptors, tool_name_prefix=True)

        # Get all tools from all servers
        tools = await client.get_tools()
        logger.info(f"Successfully loaded {len(tools)} tool(s) from MCP servers")

        # Patch 工具以支持同步调用，因为 deerflow 客户端是同步流式处理的
        # 为什么：langchain-mcp-adapters 的所有 MCP 工具都将调用暴露为
        # async 协程。DeerFlow 的 subagent 执行器和同步流路径通过
        # tool.func() 同步调用工具。我们将每个协程包装在
        # _make_sync_tool_wrapper 中，使它们在同步和异步上下文中都能工作，
        # 调用者无需感知。
        for tool in tools:
            if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
                tool.func = _make_sync_tool_wrapper(tool.coroutine, tool.name)

        return tools

    except Exception as e:
        logger.error(f"Failed to load MCP tools: {e}", exc_info=True)
        return []
