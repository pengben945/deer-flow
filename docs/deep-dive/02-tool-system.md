# 工具系统 — 动态组装与多源融合

> DeerFlow Agent Harness 深度分析 · 第 2 篇

---

## 1. 概述与定位

### 在整体架构中的位置

工具系统是 Agent 的"双手"——LLM 通过工具与外部世界交互。DeerFlow 的工具系统不是简单的工具列表，而是一个**多源融合的动态组装系统**，从 5 个不同来源汇聚工具，按优先级去重，支持延迟发现和运行时热加载。

```
┌─────────────────────────────────────────────────────────────┐
│                     get_available_tools()                     │
│                                                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │  Config  │→│ Builtin  │→│   MCP    │→│   ACP    │   │
│  │  Tools   │  │  Tools   │  │  Tools   │  │  Tools   │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
│        │             │             │             │           │
│        └─────────────┴─────────────┴─────────────┘           │
│                          │                                    │
│                    去重（按名称）                              │
│                          │                                    │
│                    ┌──────────┐                               │
│                    │  Unique  │                               │
│                    │  Tools   │                               │
│                    └──────────┘                               │
└─────────────────────────────────────────────────────────────┘
```

### 解决的核心问题

1. **多源工具融合**：配置定义的、内置的、MCP 的、社区的、ACP 的——如何统一管理？
2. **动态加载**：工具实现类在运行时按路径解析，如何安全加载？
3. **上下文窗口管理**：数百个 MCP 工具的 schema 会占满上下文，如何延迟加载？
4. **命名冲突**：不同来源可能定义同名工具，如何去重？
5. **安全隔离**：本地沙箱模式下不应暴露 host-bash，如何条件过滤？

### 一句话设计哲学

**"工具是 Agent 的能力边界，动态组装是可扩展性的基石，延迟加载是效率的保障。"**

---

## 2. 架构总览

### 2.1 五层工具来源

| 层 | 来源 | 加载方式 | 示例 |
|----|------|---------|------|
| **L1: Config** | `config.yaml` 的 `tools` 列表 | `resolve_variable(cfg.use, BaseTool)` 动态加载 | `deerflow.sandbox.tools:bash_tool` |
| **L2: Builtin** | `deerflow/tools/builtins/` | Python import | `present_files`, `ask_clarification`, `task`, `view_image` |
| **L3: MCP** | MCP 服务器 | `get_cached_mcp_tools()` 缓存加载 | 任意 MCP 工具 |
| **L4: Community** | `deerflow/community/` | 通过 Config 引用 | `web_search`, `web_fetch` |
| **L5: ACP** | ACP 外部智能体 | `build_invoke_acp_agent_tool()` 动态构建 | `invoke_acp_agent` |

**注意**：Community 工具实际上通过 Config 层引用（在 `config.yaml` 中声明 `use: deerflow.community.tavily:web_search_tool`），所以运行时是 4 层：Config + Builtin + MCP + ACP。

### 2.2 工具组装完整流程

```python
def get_available_tools(
    groups: list[str] | None = None,       # 工具分组过滤
    include_mcp: bool = True,              # 是否包含 MCP
    model_name: str | None = None,         # 模型名（决定 vision 工具）
    subagent_enabled: bool = False,        # 是否包含子智能体工具
    *,
    app_config: AppConfig | None = None,
) -> list[BaseTool]:
```

```
get_available_tools()
  │
  ├── 1. 读取 config.tools，按 groups 过滤
  ├── 2. 过滤 host-bash（LocalSandboxProvider 模式下）
  ├── 3. resolve_variable(cfg.use, BaseTool) → 动态加载 Config 工具
  ├── 4. 名称不匹配警告（config name ≠ tool.name）
  │
  ├── 5. 组装 Builtin 工具：
  │     ├── present_files, ask_clarification（始终包含）
  │     ├── skill_manage（skill_evolution.enabled 时）
  │     ├── task（subagent_enabled 时）
  │     ├── view_image（模型 supports_vision 时）
  │     └── tool_search（tool_search.enabled + MCP 工具存在时）
  │
  ├── 6. 加载 MCP 工具：
  │     ├── ExtensionsConfig.from_file()（每次重读，热重载）
  │     ├── get_cached_mcp_tools()（mtime 缓存）
  │     └── tool_search.enabled → DeferredToolRegistry 注册
  │
  ├── 7. 构建 ACP 工具：
  │     └── build_invoke_acp_agent_tool(acp_agents)
  │
  └── 8. 去重：按 tool.name，优先级 Config > Builtin > MCP > ACP
```

---

## 3. 源码走读

### 3.1 动态类加载：`resolve_variable()`

**文件**：`deerflow/reflection/resolvers.py`

这是整个工具系统（以及模型工厂）的基础——将字符串路径解析为 Python 对象。

```python
def resolve_variable[T](
    variable_path: str,
    expected_type: type[T] | tuple[type, ...] | None = None,
) -> T:
    """Resolve a variable from a path.

    Args:
        variable_path: "module.path:variable_name"
        expected_type: Optional type validation

    Returns:
        The resolved variable.
    """
    # 1. 分割模块路径和变量名
    try:
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(
            f"{variable_path} doesn't look like a variable path. "
            f"Example: parent_package_name.sub_package_name.module_name:variable_name"
        ) from err

    # 2. 动态导入模块
    try:
        module = import_module(module_path)
    except ImportError as err:
        # 3. 构建可操作的缺失依赖提示
        module_root = module_path.split(".", 1)[0]
        err_name = getattr(err, "name", None)
        if isinstance(err, ModuleNotFoundError) or err_name == module_root:
            hint = _build_missing_dependency_hint(module_path, err)
            raise ImportError(
                f"Could not import module {module_path}. {hint}"
            ) from err
        raise ImportError(
            f"Error importing module {module_path}: {err}"
        ) from err

    # 4. 获取属性
    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        raise ImportError(
            f"Module {module_path} does not define a {variable_name} attribute/class"
        ) from err

    # 5. 类型验证
    if expected_type is not None:
        if not isinstance(variable, expected_type):
            type_name = (
                expected_type.__name__
                if isinstance(expected_type, type)
                else " or ".join(t.__name__ for t in expected_type)
            )
            raise ValueError(
                f"{variable_path} is not an instance of {type_name}, "
                f"got {type(variable).__name__}"
            )

    return variable
```

**路径格式**：`module_path:variable_name`，冒号分割。例如：
- `deerflow.sandbox.tools:bash_tool` → `from deerflow.sandbox.tools import bash_tool`
- `langchain_openai:ChatOpenAI` → `from langchain_openai import ChatOpenAI`

**缺失依赖提示**：

```python
MODULE_TO_PACKAGE_HINTS = {
    "langchain_google_genai": "langchain-google-genai",
    "langchain_anthropic": "langchain-anthropic",
    "langchain_openai": "langchain-openai",
    "langchain_deepseek": "langchain-deepseek",
}
```

当导入失败时，不是抛一个裸 `ImportError`，而是生成可操作的提示：`"Missing dependency 'langchain_openai'. Install it with uv add langchain-openai"`。这在生产环境中极大降低了调试成本。

**`resolve_class()` 的区别**：

```python
def resolve_class[T](class_path: str, base_class: type[T] | None = None) -> type[T]:
    model_class = resolve_variable(class_path, expected_type=type)  # 验证是类
    if base_class is not None and not issubclass(model_class, base_class):
        raise ValueError(f"{class_path} is not a subclass of {base_class.__name__}")
    return model_class
```

`resolve_variable` 用 `isinstance()` 验证实例类型，`resolve_class` 额外用 `issubclass()` 验证继承关系。

### 3.2 Config 工具加载

在 `config.yaml` 中声明工具：

```yaml
tools:
  - name: bash
    use: deerflow.sandbox.tools:bash_tool
    group: bash
  - name: read_file
    use: deerflow.sandbox.tools:read_file_tool
    group: file
  - name: web_search
    use: deerflow.community.tavily:web_search_tool
    group: search
```

加载流程：

```python
# 1. 读取配置
config = app_config or get_app_config()
tool_configs = [tool for tool in config.tools
                if groups is None or tool.group in groups]

# 2. 安全过滤：LocalSandboxProvider 模式下不暴露 host-bash
if not is_host_bash_allowed(config):
    tool_configs = [tool for tool in tool_configs
                    if not _is_host_bash_tool(tool)]

# 3. 动态加载
loaded_tools_raw = [(cfg, resolve_variable(cfg.use, BaseTool))
                     for cfg in tool_configs]

# 4. 名称不匹配警告
for cfg, loaded in loaded_tools_raw:
    if cfg.name != loaded.name:
        logger.warning(
            "Tool name mismatch: config name %r does not match tool .name %r",
            cfg.name, loaded.name, cfg.use,
        )
```

**Host-bash 过滤的动机**：在 `LocalSandboxProvider` 模式下，bash 工具直接在宿主机执行命令，没有容器隔离。暴露给 Agent 意味着它可以执行任意宿主机命令——这是严重的安全风险。`is_host_bash_allowed()` 检查配置是否显式允许。

### 3.3 内置工具详解

#### 3.3.1 `ask_clarification` — 澄清请求

```python
@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(
    question: str,
    clarification_type: Literal[
        "missing_info",           # 缺少信息
        "ambiguous_requirement",  # 需求模糊
        "approach_choice",        # 方案选择
        "risk_confirmation",      # 风险确认
        "suggestion",             # 建议
    ],
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    # 占位实现——实际由 ClarificationMiddleware 拦截
    return "Clarification request processed by middleware"
```

**关键设计**：
- `return_direct=True`：工具输出直接返回给用户，不经过 Agent 推理
- 占位返回永远不会执行——`ClarificationMiddleware` 在 `wrap_tool_call` 中拦截
- 5 种澄清类型对应不同的 UI 图标（❓🤔🔀⚠️💡）

**这种"工具声明 + 中间件拦截"的模式**值得注意：工具的存在让 LLM 知道可以请求澄清（schema 出现在工具列表中），但实际执行被中间件接管，实现了"声明与执行分离"。

#### 3.3.2 `present_files` — 文件展示

```python
@tool("present_files", parse_docstring=True)
def present_file_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    filepaths: list[str],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    normalized_paths = [_normalize_presented_filepath(runtime, fp) for fp in filepaths]
    return Command(
        update={
            "artifacts": normalized_paths,  # 更新 artifacts 状态
            "messages": [ToolMessage("Successfully presented files",
                                     tool_call_id=tool_call_id)],
        },
    )
```

**关键设计**：
- 返回 `Command` 而非 `str`——同时更新 `artifacts` 和 `messages` 两个 state 键
- 路径归一化：只接受 `/mnt/user-data/outputs/*` 下的文件
- `merge_artifacts` reducer 自动去重

#### 3.3.3 `task` — 子智能体委派

```python
@tool("task", parse_docstring=True)
async def task_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    description: str,          # 任务描述
    prompt: str,               # 具体指令
    subagent_type: str,        # 子智能体类型
    tool_call_id: Annotated[str, InjectedToolCallId],
    max_turns: int | None = None,  # 最大轮次
) -> str:
```

执行流程：
1. 解析 `SubagentConfig`（从注册表查找 `subagent_type`）
2. 检查 bash 权限（LocalSandboxProvider 限制）
3. 提取父上下文：`sandbox_state`, `thread_data`, `thread_id`, `parent_model`, `trace_id`
4. 获取工具集（`subagent_enabled=False`，防止嵌套）
5. 创建 `SubagentExecutor`，调用 `execute_async()`
6. 后台轮询（每 5 秒），发送流式事件（`task_started`, `task_running`, `task_completed`）
7. 处理取消：`CancelledError` → `request_cancel_background_task()` + 延迟清理

#### 3.3.4 `view_image` — 图片查看

```python
@tool("view_image", parse_docstring=True)
def view_image_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    image_path: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
```

安全限制：
- 只允许 `/mnt/user-data/{workspace,uploads,outputs}` 下的图片
- 最大 20MB
- Magic-byte 验证（文件内容必须匹配声明扩展名）
- 支持 `.jpg`, `.jpeg`, `.png`, `.webp`

返回 `Command` 更新 `viewed_images` 字典（路径 → `{base64, mime_type}`）。

#### 3.3.5 `setup_agent` / `update_agent` — 自定义智能体管理

- `setup_agent`：Bootstrap 模式专用，创建 `SOUL.md` + `config.yaml`
- `update_agent`：自定义智能体自更新，**原子写入**（stage temp → `Path.replace`）

```python
def _stage_temp(path: Path, text: str) -> Path:
    """Write text into a sibling temp file and return its path."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    return tmp

# 原子提交
for staged, target in zip(staged_files, target_files):
    staged.replace(target)  # POSIX atomic rename
```

### 3.4 延迟工具搜索：DeferredToolRegistry

**文件**：`deerflow/tools/builtins/tool_search.py`

**问题**：当 MCP 服务器提供数百个工具时，所有 schema 一起注入上下文会占满 token 预算。

**解决方案**：延迟加载——MCP 工具的 schema 不直接暴露给 LLM，而是注册到 `DeferredToolRegistry`，LLM 通过 `tool_search` 工具按需发现。

```python
@dataclass
class DeferredToolEntry:
    name: str
    description: str
    tool: BaseTool  # 完整工具对象，仅搜索匹配时返回

class DeferredToolRegistry:
    def __init__(self):
        self._entries: list[DeferredToolEntry] = []

    def register(self, tool: BaseTool) -> None:
        """注册工具到延迟注册表。"""
        self._entries.append(DeferredToolEntry(
            name=tool.name,
            description=tool.description,
            tool=tool,
        ))

    def promote(self, names: set[str]) -> None:
        """将工具从延迟集合提升为可见。"""
        # promote 后，DeferredToolFilterMiddleware 不再过滤这些工具

    def search(self, query: str) -> list[BaseTool]:
        """搜索延迟工具，支持三种查询形式。"""
        # "select:name1,name2" → 精确名称匹配
        # "+keyword rest"     → 名称必须包含 keyword，按 rest 排名
        # "keyword query"     → 正则匹配 name + description
```

**ContextVar 隔离**：

```python
_registry_var: contextvars.ContextVar[DeferredToolRegistry | None] = \
    contextvars.ContextVar("deferred_tool_registry", default=None)
```

每个 LangGraph 运行有独立的注册表实例，避免并发请求间的状态污染。

**tool_search 工具**：

```python
@tool
def tool_search(query: str) -> str:
    registry = get_deferred_registry()
    matched_tools = registry.search(query)
    tool_defs = [convert_to_openai_function(t) for t in matched_tools[:MAX_RESULTS]]
    registry.promote({t.name for t in matched_tools[:MAX_RESULTS]})  # 提升为可见
    return json.dumps(tool_defs, indent=2, ensure_ascii=False)
```

**流程**：
1. Agent 调用 `tool_search("slack send message")`
2. Registry 搜索匹配的工具（最多 5 个）
3. 返回 OpenAI function-calling 格式的 schema
4. `promote()` 将匹配工具从延迟集合移除
5. 下次 `DeferredToolFilterMiddleware` 运行时，这些工具不再被过滤
6. Agent 可以直接调用这些工具

### 3.5 ACP 工具：`invoke_acp_agent`

**文件**：`deerflow/tools/builtins/invoke_acp_agent_tool.py`

ACP (Agent Communication Protocol) 是 Anthropic 提出的智能体间通信协议。DeerFlow 通过 `invoke_acp_agent` 工具调用外部 ACP 兼容智能体。

```python
def build_invoke_acp_agent_tool(agents: dict) -> BaseTool:
    """动态构建 invoke_acp_agent 工具。"""
    # 1. 生成描述（列出可用智能体）
    # 2. 定义 _invoke() 协程
    #    - 创建 per-thread 工作区
    #    - 启动 ACP 智能体进程
    #    - 初始化会话
    #    - 发送 prompt
    #    - 收集流式响应
    # 3. 返回 StructuredTool.from_function(coroutine=_invoke)
```

**关键特性**：
- **动态构建**：工具的描述和可用智能体列表在运行时生成
- **Per-thread 隔离**：每个线程有独立的 ACP 工作区
- **MCP 转发**：将 DeerFlow 的 MCP 服务器配置转发给 ACP 智能体
- **权限处理**：`auto_approve_permissions` 配置自动批准或取消

### 3.6 社区工具示例：Tavily

**文件**：`deerflow/community/tavily/tools.py`

```python
@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    config = get_app_config().get_tool_config("web_search")
    max_results = 5
    if config is not None and "max_results" in config.model_extra:
        max_results = config.model_extra.get("max_results")

    client = _get_tavily_client()
    res = client.search(query, max_results=max_results)
    normalized_results = [
        {
            "title": result["title"],
            "url": result["url"],
            "snippet": result["content"],
        }
        for result in res["results"]
    ]
    return json.dumps(normalized_results, indent=2, ensure_ascii=False)
```

**社区工具一览**：

| 工具 | 目录 | 能力 |
|------|------|------|
| Tavily | `community/tavily/` | web_search + web_fetch |
| Jina AI | `community/jina_ai/` | reader + search |
| Firecrawl | `community/firecrawl/` | web scraping |
| DuckDuckGo | `community/ddg_search/` | 搜索（无需 API key） |
| Exa | `community/exa/` | 语义搜索 |
| Image Search | `community/image_search/` | 图片搜索 |
| InfoQuest | `community/infoquest/` | 深度搜索 |
| Serper | `community/serper/` | Google 搜索 API |

---

## 4. 核心机制详解

### 4.1 工具去重策略

```python
all_tools = loaded_tools + builtin_tools + mcp_tools + acp_tools
seen_names: set[str] = set()
unique_tools: list[BaseTool] = []
for t in all_tools:
    if t.name not in seen_names:
        unique_tools.append(t)
        seen_names.add(t.name)
    else:
        logger.warning(
            "Duplicate tool name %r detected and skipped — "
            "check your config.yaml and MCP server registrations (issue #1803).",
            t.name,
        )
```

**优先级**：Config > Builtin > MCP > ACP（连接顺序决定，首次出现胜出）。

**为什么 Config 优先？** 用户在 `config.yaml` 中显式声明的工具代表有意的配置选择，应该覆盖同名内置工具。例如，用户可能配置了自定义的 `web_search` 实现，不应被内置版本覆盖。

### 4.2 Host-bash 安全过滤

```python
def _is_host_bash_tool(tool: object) -> bool:
    group = getattr(tool, "group", None)
    use = getattr(tool, "use", None)
    if group == "bash":
        return True
    if use == "deerflow.sandbox.tools:bash_tool":
        return True
    return False
```

```python
if not is_host_bash_allowed(config):
    tool_configs = [tool for tool in tool_configs
                    if not _is_host_bash_tool(tool)]
```

**设计动机**：`LocalSandboxProvider` 在宿主机直接执行命令，无容器隔离。如果 Agent 可以执行 `bash`，等于可以执行任意宿主机命令（`rm -rf /`、读取 `/etc/passwd` 等）。只有在 `AioSandboxProvider`（Docker 隔离）或用户显式允许时，host-bash 才会暴露。

### 4.3 条件工具注入

工具不是静态的——根据运行时条件动态决定是否包含：

| 条件 | 工具 | 判断逻辑 |
|------|------|---------|
| `subagent_enabled=True` | `task` | 运行时参数 |
| `model.supports_vision=True` | `view_image` | 模型配置 |
| `skill_evolution.enabled=True` | `skill_manage` | 应用配置 |
| `tool_search.enabled=True` + MCP 工具存在 | `tool_search` | 应用配置 + 运行时状态 |
| `acp_agents` 非空 | `invoke_acp_agent` | 应用配置 |
| `is_bootstrap=True` | 仅 `setup_agent` | Bootstrap 模式 |
| `agent_name` 已设置 | `update_agent` | 自定义智能体模式 |

### 4.4 MCP 热重载

```python
# 每次调用都重读 extensions_config.json
from deerflow.config.extensions_config import ExtensionsConfig
extensions_config = ExtensionsConfig.from_file()  # ← 从磁盘读取
if extensions_config.get_enabled_mcp_servers():
    mcp_tools = get_cached_mcp_tools()  # ← mtime 缓存
```

**为什么每次重读？** Gateway API 的 `PUT /api/mcp` 端点在独立进程中修改 `extensions_config.json`。如果缓存配置对象，Gateway 的修改不会生效。重读文件确保热重载。

**mtime 缓存**：`get_cached_mcp_tools()` 检查文件的修改时间，只在文件变化时重新初始化 MCP 连接，避免每次请求都重建连接。

---

## 5. 设计模式提取

### 5.1 策略模式（Strategy）

工具本身是策略——LLM 选择调用哪个工具，工具执行具体逻辑。`BaseTool` 是策略接口，每个工具实现是策略实现。

### 5.2 工厂模式（Factory）

`get_available_tools()` 是工厂方法，根据配置和运行时条件组装工具列表。`build_invoke_acp_agent_tool()` 是更细粒度的工厂，动态构建 ACP 工具。

### 5.3 代理模式（Proxy）

`ask_clarification` 工具是代理模式的变体——它声明了接口（schema），但实际执行由 `ClarificationMiddleware` 代理。LLM 看到的是工具，用户看到的是中断提示。

### 5.4 注册表模式（Registry）

`DeferredToolRegistry` 是注册表模式的实现——工具注册到注册表，通过名称或模式搜索查找。`SubagentConfig` 的注册表也是类似模式。

### 5.5 虚拟代理模式（Virtual Proxy）

`tool_search` 实现了虚拟代理——MCP 工具的完整 schema 不直接加载，而是通过搜索按需获取。这是"延迟初始化"的经典应用。

---

## 6. 业界对比

### 6.1 Agent 框架工具系统对比

| 特性 | DeerFlow | LangChain | CrewAI | OpenAI Function Calling |
|------|---------|-----------|--------|------------------------|
| **工具来源** | 5 层（Config/Builtin/MCP/Community/ACP） | 1 层（Python 函数） | 1 层（Python 类） | 1 层（JSON schema） |
| **动态加载** | `resolve_variable()` 字符串路径 | Python import | Python import | N/A（API 侧） |
| **延迟发现** | DeferredToolRegistry + tool_search | 无 | 无 | 无 |
| **去重** | 按名称，优先级排序 | 无 | 无 | 无 |
| **安全过滤** | Host-bash 条件过滤 | 无 | 无 | 无 |
| **MCP 集成** | 原生（langchain-mcp-adapters） | 原生 | 无 | 无 |
| **运行时热加载** | mtime 缓存 + 文件重读 | 无 | 无 | N/A |

### 6.2 动态加载机制对比

| 机制 | DeerFlow `resolve_variable` | Django `import_string` | Pluggy | setuptools entry_points |
|------|---------------------------|----------------------|--------|------------------------|
| **路径格式** | `module:variable` | `module.variable` | hook 标记 | `group:name = module:attr` |
| **类型验证** | `expected_type` 参数 | 无 | 无 | 无 |
| **依赖提示** | 可操作的安装命令 | 无 | 无 | 无 |
| **泛型返回** | `resolve_variable[T]` | 无 | 无 | 无 |

DeerFlow 的 `resolve_variable` 相比 Django 的 `import_string` 多了类型验证和依赖提示，这在生产环境中更有价值。

---

## 7. 面试关联

### Q1: Agent 工具系统的设计原则是什么？

**标准回答**：

工具应该有清晰的 schema、单一职责、幂等性（如果可能）。

**加分项**：

> "在我分析的 DeerFlow 项目中，工具系统有五个值得注意的设计原则：一是**多源融合与优先级去重**——工具来自 Config/Builtin/MCP/ACP 四个来源，按连接顺序去重（Config 优先），因为用户显式配置应该覆盖默认实现；二是**条件注入**——工具不是静态列表，而是根据运行时条件动态决定是否包含（如 `view_image` 仅对视觉模型、`task` 仅在子智能体启用时）；三是**声明与执行分离**——`ask_clarification` 工具声明了 schema 让 LLM 知道可以请求澄清，但实际执行被 ClarificationMiddleware 拦截，实现了接口与实现的解耦；四是**延迟发现**——数百个 MCP 工具的 schema 不直接注入上下文，而是注册到 DeferredToolRegistry，LLM 通过 `tool_search` 按需发现，节省 token 预算；五是**安全过滤**——LocalSandboxProvider 模式下自动过滤 host-bash 工具，防止 Agent 在无隔离的宿主机上执行任意命令。"

### Q2: 动态加载的安全性考虑？

**标准回答**：

动态加载需要验证类型、限制可加载的模块、处理导入错误。

**加分项**：

> "DeerFlow 的 `resolve_variable()` 有三层安全：一是**类型验证**——`expected_type` 参数用 `isinstance()` 检查，`resolve_class()` 额外用 `issubclass()` 检查继承关系，防止加载了错误类型的对象；二是**可操作的错误提示**——不是抛裸 `ImportError`，而是映射模块名到包名（`langchain_openai` → `langchain-openai`），生成 `uv add langchain-openai` 这样的安装命令；三是**路径格式校验**——冒号分割的 `module:variable` 格式，不符合格式立即报错而非尝试导入。但值得注意的是，DeerFlow 没有限制可加载的模块范围——任何 `config.yaml` 中的 `use` 路径都会被解析。在生产环境中，可能需要白名单机制限制可加载的模块。"

### Q3: 如何解决工具数量过多导致的上下文窗口问题？

**标准回答**：

限制工具数量、合并相似工具、使用工具选择策略。

**加分项**：

> "DeerFlow 用**延迟工具搜索**解决这个问题。当 `tool_search.enabled` 时，MCP 工具不直接暴露 schema，而是注册到 DeferredToolRegistry。LLM 看到一个 `tool_search` 工具，调用它搜索需要的工具，搜索结果返回 OpenAI function-calling 格式的 schema，同时 `promote()` 将匹配工具从延迟集合移除，后续可以直接调用。这实现了**两阶段工具发现**：第一阶段用轻量描述搜索，第二阶段加载完整 schema。每次搜索最多返回 5 个工具，避免一次性注入过多 schema。ContextVar 隔离确保并发请求间不互相污染。"

### Q4: 如何处理工具命名冲突？

**标准回答**：

使用命名空间前缀、报错提示、或优先级覆盖。

**加分项**：

> "DeerFlow 采用**优先级覆盖 + 警告日志**策略。工具按 Config → Builtin → MCP → ACP 顺序连接，同名工具首次出现胜出，后续重复被跳过并记录警告日志（关联 issue #1803）。Config 优先的设计理由是：用户在 config.yaml 中显式声明的工具代表有意的配置选择——例如用户可能配置了自定义的 `web_search`（使用不同的搜索 API），不应被内置或 MCP 的同名工具覆盖。这种'配置覆盖默认'的哲学在 12-Factor App 中也有体现。"

---

## 8. 扩展思考

### 8.1 局限与改进方向

| 局限 | 改进方向 |
|------|---------|
| 动态加载无模块白名单 | 添加 `allowed_tool_modules` 配置，限制可加载范围 |
| 工具 schema 无法运行时修改 | 支持 `tool_schema_override` 配置，允许调整参数描述 |
| 去重仅按名称，不考虑版本 | 支持 `tool.name:v2` 版本化命名 |
| DeferredToolRegistry 搜索仅支持正则 | 添加语义搜索（embedding-based） |
| 社区工具无统一接口规范 | 定义 `DeerFlowTool` 基类，标准化配置读取和错误处理 |
| ACP 工具无超时控制 | 添加 per-agent timeout 配置 |

### 8.2 如果重新设计

1. **工具注册中心**：类似 npm registry 的工具注册中心，支持版本、依赖、权限声明
2. **工具能力声明**：每个工具声明 `capabilities`（如 `file_read`, `network_access`），护栏基于能力而非工具名
3. **工具组合**：支持工具管道（tool_1 | tool_2），类似 Unix 管道
4. **工具沙箱分级**：不同工具在不同隔离级别执行（网络工具在容器中，文件工具在沙箱中，纯计算工具在进程内）
5. **工具指标**：自动记录每个工具的调用次数、延迟、错误率、token 消耗

### 8.3 与前沿研究/产品的关联

- **MCP 协议**：Anthropic 的 Model Context Protocol 正在成为工具发现和集成的标准，DeerFlow 的 MCP 集成是前瞻性设计
- **OpenAI Function Calling**：DeerFlow 的 `convert_to_openai_function()` 兼容 OpenAI 格式，确保跨模型可用
- **LangChain Tool Binding**：DeerFlow 基于 LangChain 的 `BaseTool` 抽象，继承了生态兼容性
- **ACP 协议**：Agent Communication Protocol 是智能体间协作的新标准，DeerFlow 的 ACP 集成支持跨框架智能体调用
