# MCP 集成 — 模型上下文协议的工程化

> DeerFlow Agent Harness 深度分析 · 第 6 篇

---

## 1. 概述与定位

### 在整体架构中的位置

MCP (Model Context Protocol) 是 Anthropic 提出的工具集成标准协议。DeerFlow 通过 `langchain-mcp-adapters` 实现了 MCP 的完整工程化：多服务器管理、OAuth 认证、缓存失效、延迟发现、运行时热加载。

```
┌──────────────────────────────────────────────────────────────┐
│                    MCP Integration                            │
│                                                                │
│  extensions_config.json                                       │
│    │                                                           │
│    ├── build_servers_config()  → MultiServerMCPClient 配置     │
│    │     ├── stdio  → command + args + env                    │
│    │     ├── sse    → url + headers + OAuth                   │
│    │     └── http   → url + headers + OAuth                   │
│    │                                                           │
│    ├── OAuthTokenManager  → Bearer token 注入                  │
│    │                                                           │
│    ├── MultiServerMCPClient  → 连接 MCP 服务器                 │
│    │                                                           │
│    └── get_cached_mcp_tools() → 缓存工具列表                   │
│          │                                                     │
│          ├── [tool_search.enabled]                             │
│          │     └── DeferredToolRegistry → 延迟注册             │
│          │                                                     │
│          └── [直接注入] → Agent 工具列表                       │
└──────────────────────────────────────────────────────────────┘
```

### 解决的核心问题

1. **工具发现**：MCP 服务器动态提供工具，无需硬编码
2. **多服务器管理**：同时连接多个 MCP 服务器（stdio/SSE/HTTP）
3. **认证**：OAuth 2.0 token 获取、缓存、自动刷新
4. **缓存与热加载**：mtime 失效检测，Gateway API 修改后自动生效
5. **上下文管理**：延迟发现避免数百个工具 schema 占满上下文

### 一句话设计哲学

**"MCP 是工具集成的 USB 接口——即插即用，协议标准化，实现可替换。"**

---

## 2. 架构总览

### 2.1 四组件架构

| 组件 | 文件 | 职责 |
|------|------|------|
| **client.py** | `mcp/client.py` | 构建 MultiServerMCPClient 配置 |
| **tools.py** | `mcp/tools.py` | 工具加载、拦截器注入、同步包装 |
| **cache.py** | `mcp/cache.py` | mtime 缓存 + 懒初始化 |
| **oauth.py** | `mcp/oauth.py` | OAuth 2.0 token 管理 |

### 2.2 完整加载流程

```
get_cached_mcp_tools()
  │
  ├── _is_cache_stale()? → 是 → reset_mcp_tools_cache()
  │
  ├── 未初始化? → initialize_mcp_tools()
  │     │
  │     └── get_mcp_tools()
  │           │
  │           ├── ExtensionsConfig.from_file()  → 重读配置（热加载）
  │           ├── build_servers_config()         → 构建服务器参数
  │           ├── get_initial_oauth_headers()    → 预取 OAuth token
  │           ├── build_oauth_tool_interceptor() → 构建 OAuth 拦截器
  │           ├── [自定义拦截器]                → resolve_variable() 加载
  │           ├── MultiServerMCPClient(config)   → 创建客户端
  │           ├── client.get_tools()             → 获取工具列表
  │           └── _make_sync_tool_wrapper()      → 同步包装
  │
  └── 返回缓存的工具列表
```

---

## 3. 源码走读

### 3.1 服务器配置构建：`build_server_params()`

```python
def build_server_params(server_name: str, config: McpServerConfig) -> dict[str, Any]:
    """将内部 McpServerConfig 转换为 MultiServerMCPClient 参数。"""
    transport = config.type or "stdio"

    if transport == "stdio":
        if not config.command:
            raise ValueError(f"MCP server '{server_name}': stdio requires 'command'")
        params = {
            "transport": "stdio",
            "command": config.command,
            "args": config.args or [],
        }
        if config.env:
            params["env"] = config.env
        return params

    elif transport in ("sse", "http"):
        if not config.url:
            raise ValueError(f"MCP server '{server_name}': {transport} requires 'url'")
        params = {
            "transport": transport,
            "url": config.url,
        }
        if config.headers:
            params["headers"] = config.headers
        return params

    else:
        raise ValueError(f"MCP server '{server_name}': unsupported transport '{transport}'")
```

**三种传输协议**：

| 协议 | 配置要求 | 适用场景 |
|------|---------|---------|
| **stdio** | `command` + `args` + `env` | 本地进程（如 `npx @anthropic/mcp-server-filesystem`） |
| **sse** | `url` + `headers` | Server-Sent Events 远程服务器 |
| **http** | `url` + `headers` | HTTP 远程服务器 |

### 3.2 工具加载：`get_mcp_tools()`

```python
async def get_mcp_tools() -> list[BaseTool]:
    """从所有配置的 MCP 服务器加载工具。"""
    try:
        from langchain_mcp_adapters import MultiServerMCPClient
    except ImportError:
        return []

    # 1. 重读配置（热加载）
    extensions_config = ExtensionsConfig.from_file()
    servers_config = build_servers_config(extensions_config)
    if not servers_config:
        return []

    # 2. 预取 OAuth token
    oauth_headers = await get_initial_oauth_headers(extensions_config)
    for server_name, header_value in oauth_headers.items():
        if server_name in servers_config:
            headers = servers_config[server_name].setdefault("headers", {})
            headers["Authorization"] = header_value

    # 3. 构建拦截器
    tool_interceptors = []
    oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
    if oauth_interceptor:
        tool_interceptors.append(oauth_interceptor)

    # 4. 自定义拦截器
    custom_interceptor_paths = extensions_config.model_extra.get("mcpInterceptors", [])
    for path in custom_interceptor_paths:
        builder = resolve_variable(path)
        interceptor = builder() if callable(builder) else builder
        if callable(interceptor):
            tool_interceptors.append(interceptor)

    # 5. 创建客户端
    client = MultiServerMCPClient(
        servers_config,
        tool_interceptors=tool_interceptors,
        tool_name_prefix=True,  # 按服务器名命名空间
    )

    # 6. 获取工具
    tools = await client.get_tools()

    # 7. 同步包装
    for tool in tools:
        if tool.func is None and tool.coroutine is not None:
            tool.func = _make_sync_tool_wrapper(tool.coroutine, tool.name)

    return tools
```

**关键设计**：
- **每次重读配置**：`ExtensionsConfig.from_file()` 不使用缓存，确保 Gateway API 的修改生效
- **`tool_name_prefix=True`**：工具名按服务器名命名空间（如 `filesystem__read_file`），避免冲突
- **同步包装**：LangGraph 的同步路径需要 `tool.func`，MCP 工具只有 `tool.coroutine`

### 3.3 同步包装：`_make_sync_tool_wrapper()`

```python
_SYNC_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="mcp-sync-tool")

def _make_sync_tool_wrapper(coro, tool_name):
    """将异步 MCP 工具包装为同步可调用。"""
    def sync_wrapper(*args, **kwargs):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # 已有事件循环运行 → 在线程池中执行
            future = _SYNC_TOOL_EXECUTOR.submit(
                asyncio.run, coro(*args, **kwargs)
            )
            return future.result()
        else:
            # 无事件循环 → 直接执行
            return asyncio.run(coro(*args, **kwargs))

    return sync_wrapper
```

**嵌套事件循环问题**：Python 不允许在已运行的事件循环中调用 `asyncio.run()`。解决方案是在线程池中运行新的独立事件循环。

### 3.4 mtime 缓存：`get_cached_mcp_tools()`

```python
_mcp_tools_cache: list[BaseTool] | None = None
_cache_initialized: bool = False
_initialization_lock: asyncio.Lock = asyncio.Lock()
_config_mtime: float | None = None

def _is_cache_stale() -> bool:
    """检查配置文件是否被修改。"""
    if not _cache_initialized:
        return False
    current_mtime = _get_config_mtime()
    return current_mtime != _config_mtime

def get_cached_mcp_tools() -> list[BaseTool]:
    """同步入口点，返回缓存的 MCP 工具。"""
    if _is_cache_stale():
        reset_mcp_tools_cache()

    if not _cache_initialized:
        # 懒初始化：处理不同运行时环境
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # LangGraph Studio：已有事件循环
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, initialize_mcp_tools()).result()
        else:
            asyncio.run(initialize_mcp_tools())

    return _mcp_tools_cache or []
```

**缓存失效机制**：比较 `extensions_config.json` 的 mtime。如果 Gateway API 修改了配置（`PUT /api/mcp`），mtime 变化，下次调用自动重新加载。

### 3.5 OAuth 2.0 Token 管理

```python
class OAuthTokenManager:
    def __init__(self, oauth_by_server: dict[str, McpOAuthConfig]):
        self._oauth_by_server = oauth_by_server
        self._tokens: dict[str, _OAuthToken] = {}
        self._locks: dict[str, asyncio.Lock] = {
            name: asyncio.Lock() for name in oauth_by_server
        }

    async def get_authorization_header(self, server_name: str) -> str | None:
        """获取 Bearer token，自动刷新。"""
        oauth = self._oauth_by_server.get(server_name)
        if not oauth:
            return None

        # 双重检查锁定
        token = self._tokens.get(server_name)
        if token and not self._is_expiring(token, oauth):
            return f"{token.token_type} {token.access_token}"

        async with self._locks[server_name]:
            # 再次检查（另一个协程可能已刷新）
            token = self._tokens.get(server_name)
            if token and not self._is_expiring(token, oauth):
                return f"{token.token_type} {token.access_token}"

            token = await self._fetch_token(oauth)
            self._tokens[server_name] = token
            return f"{token.token_type} {token.access_token}"

    async def _fetch_token(self, oauth: McpOAuthConfig) -> _OAuthToken:
        """从 token_url 获取新 token。"""
        async with httpx.AsyncClient(timeout=15.0) as client:
            if oauth.grant_type == "client_credentials":
                payload = {
                    "grant_type": "client_credentials",
                    "client_id": oauth.client_id,
                    "client_secret": oauth.client_secret,
                }
            elif oauth.grant_type == "refresh_token":
                payload = {
                    "grant_type": "refresh_token",
                    "refresh_token": oauth.refresh_token,
                }
                if oauth.client_id:
                    payload["client_id"] = oauth.client_id
                if oauth.client_secret:
                    payload["client_secret"] = oauth.client_secret

            response = await client.post(oauth.token_url, data=payload)
            response.raise_for_status()
            data = response.json()

            return _OAuthToken(
                access_token=data[oauth.token_field],
                token_type=data.get(oauth.token_type_field, "Bearer"),
                expires_at=datetime.now(UTC) + timedelta(
                    seconds=data.get(oauth.expires_in_field, 3600)
                ),
            )
```

**关键设计**：
- **双重检查锁定**：先无锁检查，再加锁检查，避免不必要的锁竞争
- **提前刷新**：`_is_expiring()` 检查 `expires_at <= now + refresh_skew_seconds`，在 token 过期前提前刷新
- **Per-server 锁**：每个服务器独立的 `asyncio.Lock`，不同服务器可以并发刷新
- **可配置字段名**：`token_field`、`token_type_field`、`expires_in_field` 支持非标准 OAuth 响应

---

## 4. 核心机制详解

### 4.1 拦截器链

MCP 工具调用经过拦截器链：

```
tool_call
  │
  ├── OAuth 拦截器 → 注入 Authorization header
  ├── 自定义拦截器 1 → 自定义逻辑
  ├── 自定义拦截器 2 → 自定义逻辑
  │
  └── 实际 MCP 工具执行
```

**OAuth 拦截器**：

```python
def oauth_interceptor(request, handler):
    auth_header = await token_manager.get_authorization_header(request.server_name)
    if auth_header:
        updated_headers = {**request.headers, "Authorization": auth_header}
        return await handler(request.override(headers=updated_headers))
    return await handler(request)
```

### 4.2 热加载机制

```
Gateway API (进程 A)                LangGraph Server (进程 B)
  │                                    │
  ├── PUT /api/mcp                     ├── get_cached_mcp_tools()
  │     └── 写入 extensions_config.json │     ├── _is_cache_stale()? → mtime 变化
  │                                    │     ├── reset_mcp_tools_cache()
  │                                    │     └── initialize_mcp_tools()
  │                                    │           └── ExtensionsConfig.from_file() → 新配置
```

两个进程共享同一个 `extensions_config.json` 文件。进程 A 写入，进程 B 通过 mtime 检测变化并重新加载。

---

## 5. 设计模式提取

### 5.1 适配器模式（Adapter）

`build_server_params()` 将内部 `McpServerConfig` 适配为 `MultiServerMCPClient` 的参数格式。

### 5.2 拦截器模式（Interceptor）

OAuth 和自定义拦截器在工具调用前后执行逻辑，类似 HTTP 中间件。

### 5.3 代理模式（Proxy）

`_make_sync_tool_wrapper()` 是代理——将异步工具包装为同步接口，调用者不知道底层是异步的。

### 5.4 双重检查锁定（Double-Checked Locking）

`OAuthTokenManager.get_authorization_header()` 使用双重检查锁定优化并发性能。

---

## 6. 业界对比

| 特性 | DeerFlow MCP | LangChain MCP | Claude Desktop MCP | Cursor MCP |
|------|-------------|---------------|-------------------|-----------|
| **传输协议** | stdio/SSE/HTTP | stdio/SSE | stdio | stdio |
| **OAuth** | client_credentials + refresh_token | 无 | 无 | 无 |
| **缓存** | mtime 失效 | 无 | 无 | 无 |
| **热加载** | 支持（mtime 检测） | 无 | 重启 | 重启 |
| **延迟发现** | DeferredToolRegistry | 无 | 无 | 无 |
| **自定义拦截器** | 支持 | 无 | 无 | 无 |
| **同步包装** | 自动 | 手动 | N/A | N/A |

**DeerFlow 的独特之处**：
1. **OAuth 支持**：其他 MCP 实现不处理认证，DeerFlow 支持 client_credentials 和 refresh_token
2. **热加载**：运行时修改 MCP 配置无需重启
3. **延迟发现**：大量 MCP 工具不占满上下文

---

## 7. 面试关联

### Q1: MCP 协议的核心价值是什么？

**加分项**：

> "MCP 的核心价值是**标准化工具集成接口**——类似 LSP (Language Server Protocol) 对 IDE 的价值。没有 MCP，每个 Agent 框架需要为每个工具写适配器；有了 MCP，工具提供者只需实现一次 MCP 服务器，所有兼容框架都能使用。DeerFlow 的工程化更进一步：OAuth 认证解决了企业场景的访问控制，mtime 缓存 + 热加载解决了运行时配置更新，DeferredToolRegistry 解决了工具数量过多时的上下文管理。"

### Q2: 工具发现与动态集成的挑战？

**加分项**：

> "三个核心挑战：一是**上下文窗口限制**——数百个 MCP 工具的 schema 一起注入会占满 token 预算。DeerFlow 用 DeferredToolRegistry + tool_search 实现两阶段发现：先搜索（轻量描述），再加载（完整 schema）。二是**配置一致性**——Gateway API 和 LangGraph Server 在不同进程，配置修改需要跨进程传播。DeerFlow 用文件 mtime 检测实现热加载。三是**认证管理**——OAuth token 需要获取、缓存、自动刷新。DeerFlow 用双重检查锁定 + 提前刷新策略，确保高并发下不重复获取 token。"

---

## 8. 扩展思考

### 8.1 局限与改进方向

| 局限 | 改进方向 |
|------|---------|
| 仅支持 OAuth 2.0 | 支持 API Key、Basic Auth、mTLS |
| 无 MCP 服务器健康检查 | 添加心跳 + 自动重连 |
| 无工具调用限流 | 添加 per-server rate limiting |
| 缓存仅 mtime 检测 | 添加显式缓存失效 API |
| 无 MCP 服务器沙箱 | 限制 MCP 服务器的文件系统/网络访问 |

### 8.2 与前沿研究/产品的关联

- **MCP 协议规范**：Anthropic 持续更新，DeerFlow 跟进最新版本
- **LangChain MCP Adapters**：DeerFlow 的基础依赖，未来可能支持更多传输协议
- **ACP 协议**：Agent Communication Protocol 是智能体间协作的标准，与 MCP 互补
