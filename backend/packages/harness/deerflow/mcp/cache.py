"""Cache for MCP tools to avoid repeated loading."""

import asyncio
import logging
import os

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

_mcp_tools_cache: list[BaseTool] | None = None
_cache_initialized = False
_initialization_lock = asyncio.Lock()
_config_mtime: float | None = None  # 记录配置文件修改时间


def _get_config_mtime() -> float | None:
    """Get the modification time of the extensions config file.

    Returns:
        The modification time as a float, or None if the file doesn't exist.
    """
    from deerflow.config.extensions_config import ExtensionsConfig

    config_path = ExtensionsConfig.resolve_config_path()
    if config_path and config_path.exists():
        return os.path.getmtime(config_path)
    return None


def _is_cache_stale() -> bool:
    """Check if the cache is stale due to config file changes.

    Returns:
        True if the cache should be invalidated, False otherwise.

    为什么用 mtime 而不是文件哈希：每次加载工具时读取并哈希整个配置文件
    的开销远高于 mtime 比较。代价是秒级精度：如果文件在同一秒内被写入
    两次（例如 CI 快速部署），第二次写入可能不会被检测到。对于预期的人为编辑
    和 Gateway API 更新场景（写入间隔通常数秒），这是可以接受的。
    """
    global _config_mtime

    if not _cache_initialized:
        return False  # 尚未初始化，不算过期

    current_mtime = _get_config_mtime()

    # 如果之前或现在都无法获取 mtime，假设未过期
    if _config_mtime is None or current_mtime is None:
        return False

    # 如果配置文件自缓存以来已被修改，视为过期
    if current_mtime > _config_mtime:
        logger.info(f"MCP config file has been modified (mtime: {_config_mtime} -> {current_mtime}), cache is stale")
        return True

    return False


async def initialize_mcp_tools() -> list[BaseTool]:
    """Initialize and cache MCP tools.

    This should be called once at application startup.

    Returns:
        List of LangChain tools from all enabled MCP servers.
    """
    global _mcp_tools_cache, _cache_initialized, _config_mtime

    async with _initialization_lock:
        if _cache_initialized:
            logger.info("MCP tools already initialized")
            return _mcp_tools_cache or []

        from deerflow.mcp.tools import get_mcp_tools

        logger.info("Initializing MCP tools...")
        _mcp_tools_cache = await get_mcp_tools()
        _cache_initialized = True
        _config_mtime = _get_config_mtime()  # 记录配置文件 mtime
        logger.info(f"MCP tools initialized: {len(_mcp_tools_cache)} tool(s) loaded (config mtime: {_config_mtime})")

        return _mcp_tools_cache


def get_cached_mcp_tools() -> list[BaseTool]:
    """Get cached MCP tools with lazy initialization.

    If tools are not initialized, automatically initializes them.
    This ensures MCP tools work in both FastAPI and LangGraph Studio contexts.

    Also checks if the config file has been modified since last initialization,
    and re-initializes if needed. This ensures that changes made through the
    Gateway API (which runs in a separate process) are reflected in the
    LangGraph Server.

    Returns:
        List of cached MCP tools.
    """
    global _cache_initialized

    # 检查配置文件变更是否导致缓存过期
    if _is_cache_stale():
        logger.info("MCP cache is stale, resetting for re-initialization...")
        reset_mcp_tools_cache()

    if not _cache_initialized:
        logger.info("MCP tools not initialized, performing lazy initialization...")
        try:
            # 尝试在当前事件循环中初始化
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果事件循环已在运行（例如 LangGraph Studio 中），
                # 需要在新线程中创建新的事件循环
                #
                # 为什么：不能从运行中的事件循环调用 asyncio.run()。
                # 我们将任务卸载到一个一次性 ThreadPoolExecutor，让
                # 异步 MCP 客户端初始化在它自己的事件循环中运行。
                # 边界案例：如果 MCP 服务器不可达，future.result() 会
                # 无限期阻塞调用线程——此调用没有超时机制。
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, initialize_mcp_tools())
                    future.result()
            else:
                # 如果没有运行中的事件循环，可以使用当前循环
                loop.run_until_complete(initialize_mcp_tools())
        except RuntimeError:
            # 不存在事件循环，创建一个
            try:
                asyncio.run(initialize_mcp_tools())
            except Exception:
                logger.exception("Failed to lazy-initialize MCP tools")
                return []
        except Exception:
            logger.exception("Failed to lazy-initialize MCP tools")
            return []

    return _mcp_tools_cache or []


def reset_mcp_tools_cache() -> None:
    """Reset the MCP tools cache.

    This is useful for testing or when you want to reload MCP tools.
    """
    global _mcp_tools_cache, _cache_initialized, _config_mtime
    _mcp_tools_cache = None
    _cache_initialized = False
    _config_mtime = None
    logger.info("MCP tools cache reset")
