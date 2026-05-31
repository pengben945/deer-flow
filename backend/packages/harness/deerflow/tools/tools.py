import logging

from langchain.tools import BaseTool

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.reflection import resolve_variable
from deerflow.sandbox.security import is_host_bash_allowed
from deerflow.tools.builtins import ask_clarification_tool, present_file_tool, task_tool, view_image_tool
from deerflow.tools.builtins.tool_search import reset_deferred_registry

logger = logging.getLogger(__name__)

BUILTIN_TOOLS = [
    present_file_tool,
    ask_clarification_tool,
]

SUBAGENT_TOOLS = [
    task_tool,
    # task_status_tool is no longer exposed to LLM (backend handles polling internally)
]


def _is_host_bash_tool(tool: object) -> bool:
    """如果工具配置代表一个宿主机的 bash 执行面，返回 True。

    为什么：在本地沙箱模式下，宿主机的文件系统是直接可访问的。
    暴露 bash 工具会让 agent 在宿主机上执行任意命令，破坏沙箱隔离。
    我们同时检查 ``group=="bash"``（命名约定）和具体的 ``use`` 路径（实际实现），
    因为用户可能在自定义工具组下定义 bash 工具。
    """
    group = getattr(tool, "group", None)
    use = getattr(tool, "use", None)
    if group == "bash":
        return True
    if use == "deerflow.sandbox.tools:bash_tool":
        return True
    return False


def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
) -> list[BaseTool]:
    """Get all available tools from config.

    Note: MCP tools should be initialized at application startup using
    `initialize_mcp_tools()` from deerflow.mcp module.

    Args:
        groups: Optional list of tool groups to filter by.
        include_mcp: Whether to include tools from MCP servers (default: True).
        model_name: Optional model name to determine if vision tools should be included.
        subagent_enabled: Whether to include subagent tools (task, task_status).

    Returns:
        List of available tools.
    """
    config = app_config or get_app_config()
    tool_configs = [tool for tool in config.tools if groups is None or tool.group in groups]

    # Do not expose host bash by default when LocalSandboxProvider is active.
    if not is_host_bash_allowed(config):
        tool_configs = [tool for tool in tool_configs if not _is_host_bash_tool(tool)]

    # 为什么用 resolve_variable：Config 工具以点分路径声明（例如
    # "deerflow.community.tavily.tools:web_search_tool"），用户无需接触
    # import 链即可添加工具。resolve_variable 在运行时动态导入模块，
    # 并验证结果是否为 BaseTool 子类。
    # 边界案例：如果 cfg.name != loaded.name，LLM 在 schema 中看到的是
    # 一个名称，但运行时识别的是另一个名称（问题 #1803）。
    loaded_tools_raw = [(cfg, resolve_variable(cfg.use, BaseTool)) for cfg in tool_configs]

    # Warn when the config ``name`` field and the tool object's ``.name``
    # attribute diverge — this mismatch is the root cause of issue #1803 where
    # the LLM receives one name in its tool schema but the runtime router
    # recognises a different name, producing "not a valid tool" errors.
    for cfg, loaded in loaded_tools_raw:
        if cfg.name != loaded.name:
            logger.warning(
                "Tool name mismatch: config name %r does not match tool .name %r (use: %s). The tool's own .name will be used for binding.",
                cfg.name,
                loaded.name,
                cfg.use,
            )

    loaded_tools = [t for _, t in loaded_tools_raw]

    # Conditionally add tools based on config
    builtin_tools = BUILTIN_TOOLS.copy()
    skill_evolution_config = getattr(config, "skill_evolution", None)
    if getattr(skill_evolution_config, "enabled", False):
        from deerflow.tools.skill_manage_tool import skill_manage_tool

        builtin_tools.append(skill_manage_tool)

    # Add subagent tools only if enabled via runtime parameter
    if subagent_enabled:
        builtin_tools.extend(SUBAGENT_TOOLS)
        logger.info("Including subagent tools (task)")

    # If no model_name specified, use the first model (default)
    if model_name is None and config.models:
        model_name = config.models[0].name

    # Add view_image_tool only if the model supports vision
    model_config = config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        builtin_tools.append(view_image_tool)
        logger.info(f"Including view_image_tool for model '{model_name}' (supports_vision=True)")

    # Get cached MCP tools if enabled
    # NOTE: We use ExtensionsConfig.from_file() instead of config.extensions
    # to always read the latest configuration from disk. This ensures that changes
    # made through the Gateway API (which runs in a separate process) are immediately
    # reflected when loading MCP tools.
    mcp_tools = []
    # Reset deferred registry upfront to prevent stale state from previous calls
    #
    # 为什么在这里重置：当 agent 被重新创建时（例如 MCP 配置变更后），
    # get_available_tools 会被再次调用。如果不重置，上一次调用遗留的旧 registry
    # 会作为 ContextVar 值存活在当前 asyncio 上下文中，导致 tool_search
    # 返回过期的工具引用。
    reset_deferred_registry()
    if include_mcp:
        try:
            from deerflow.config.extensions_config import ExtensionsConfig
            from deerflow.mcp.cache import get_cached_mcp_tools

            extensions_config = ExtensionsConfig.from_file()
            if extensions_config.get_enabled_mcp_servers():
                mcp_tools = get_cached_mcp_tools()
                if mcp_tools:
                    logger.info(f"Using {len(mcp_tools)} cached MCP tool(s)")

                    # When tool_search is enabled, register MCP tools in the
                    # deferred registry and add tool_search to builtin tools.
                    #
                    # 为什么：MCP 服务器可能暴露数百个工具。每轮对话都将
                    # 所有 schema 发送给 LLM 会消耗大量 Token，甚至超出上下文限制。
                    # 延迟注册表只存储工具元数据（名称 + 描述），只有当 LLM
                    # 通过 tool_search 显式获取时，才将一个工具提升到活跃 schema 列表。
                    #
                    # 这里发生了两次注册：1）注册到 DeferredToolRegistry 用于搜索；
                    # 2）DeferredToolFilterMiddleware 从 bind_tools 中剥离延迟加载的 schema。
                    if config.tool_search.enabled:
                        from deerflow.tools.builtins.tool_search import DeferredToolRegistry, set_deferred_registry
                        from deerflow.tools.builtins.tool_search import tool_search as tool_search_tool

                        registry = DeferredToolRegistry()
                        for t in mcp_tools:
                            registry.register(t)
                        set_deferred_registry(registry)
                        builtin_tools.append(tool_search_tool)
                        logger.info(f"Tool search active: {len(mcp_tools)} tools deferred")
        except ImportError:
            logger.warning("MCP module not available. Install 'langchain-mcp-adapters' package to enable MCP tools.")
        except Exception as e:
            logger.error(f"Failed to get cached MCP tools: {e}")

    # Add invoke_acp_agent tool if any ACP agents are configured
    acp_tools: list[BaseTool] = []
    try:
        from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool

        if app_config is None:
            from deerflow.config.acp_config import get_acp_agents

            acp_agents = get_acp_agents()
        else:
            acp_agents = getattr(config, "acp_agents", {}) or {}
        if acp_agents:
            acp_tools.append(build_invoke_acp_agent_tool(acp_agents))
            logger.info(f"Including invoke_acp_agent tool ({len(acp_agents)} agent(s): {list(acp_agents.keys())})")
    except Exception as e:
        logger.warning(f"Failed to load ACP tool: {e}")

    logger.info(f"Total tools loaded: {len(loaded_tools)}, built-in tools: {len(builtin_tools)}, MCP tools: {len(mcp_tools)}, ACP tools: {len(acp_tools)}")

    # Deduplicate by tool name — config-loaded tools take priority, followed by
    # built-ins, MCP tools, and ACP tools.  Duplicate names cause the LLM to
    # receive ambiguous or concatenated function schemas (issue #1803).
    #
    # 为什么是这个顺序：Config 工具由部署者精心挑选，应始终优先。
    # Builtin 工具是核心平台能力。MCP 工具是从外部服务器加载的插件，
    # 最可能产生意料之外的命名冲突。ACP 工具是外部 agent 的包装，
    # 优先级最低。
    # 边界案例：如果两个 config 工具同名，第一个 config 条目静默获胜。
    # 用户应确保在其配置中工具名唯一。
    all_tools = loaded_tools + builtin_tools + mcp_tools + acp_tools
    seen_names: set[str] = set()
    unique_tools: list[BaseTool] = []
    for t in all_tools:
        if t.name not in seen_names:
            unique_tools.append(t)
            seen_names.add(t.name)
        else:
            logger.warning(
                "Duplicate tool name %r detected and skipped — check your config.yaml and MCP server registrations (issue #1803).",
                t.name,
            )
    return unique_tools
