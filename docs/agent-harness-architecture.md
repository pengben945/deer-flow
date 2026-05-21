# DeerFlow Agent Harness 架构分析

> DeerFlow 2.0 (Deep Exploration and Efficient Research Flow) — 字节跳动开源的超级智能体框架 (Super Agent Harness)，基于 LangGraph + LangChain 构建，编排子智能体、记忆、沙箱与可扩展技能。

---

## 1. 整体架构概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Nginx (:2026)                                │
│  /api/langgraph/* → LangGraph Server  │  /api/* → Gateway (:8001)  │
└─────────────────────────────────────────────────────────────────────┘
         │                                          │
         ▼                                          ▼
┌──────────────────┐                    ┌───────────────────────────┐
│  LangGraph Server │                    │   FastAPI Gateway (:8001) │
│  (checkpoint/store)│◄──langgraph_sdk──│  Auth / Runs / SSE       │
└──────────────────┘                    │  Memory / Skills / MCP    │
                                        │  Channels / Uploads       │
                                        └──────────┬────────────────┘
                                                   │ imports
                                                   ▼
                                        ┌──────────────────────────┐
                                        │   deerflow-harness 包     │
                                        │  (可发布的核心框架)        │
                                        │                          │
                                        │  Agent / Middleware       │
                                        │  Tools / Sandbox         │
                                        │  Memory / Skills / MCP   │
                                        │  Models / Subagents      │
                                        │  Config / Runtime        │
                                        └──────────────────────────┘
                                                   │
                                                   ▼
                                        ┌──────────────────────────┐
                                        │   Frontend (Next.js)     │
                                        │   Web UI (:3000)         │
                                        └──────────────────────────┘
```

### 分层原则：Harness / App 严格边界

| 层 | 路径 | 导入前缀 | 职责 |
|---|---|---|---|
| **Harness** | `backend/packages/harness/deerflow/` | `deerflow.*` | 所有智能体编排、工具、沙箱、模型、MCP、技能、配置 |
| **App** | `backend/app/` | `app.*` | FastAPI Gateway API + IM 渠道集成 |

**依赖规则**：`app` → `deerflow`（单向），`deerflow` 不导入 `app`。CI 通过 `test_harness_boundary.py` 强制执行。

---

## 2. 技术栈

| 层 | 技术 |
|---|---|
| 后端语言 | Python 3.12+ |
| 智能体框架 | LangGraph + LangChain |
| 后端 API | FastAPI (Gateway :8001) |
| 前端 | Next.js (Node.js 22+, :3000) |
| 反向代理 | Nginx (:2026) |
| 包管理 | `uv` (Python workspace), `pnpm` (前端) |
| 沙箱 | Docker 容器 / 本地文件系统 |
| MCP | `langchain-mcp-adapters` (`MultiServerMCPClient`) |
| 追踪 | LangSmith / Langfuse |
| 持久化 | SQLite (默认), PostgreSQL (可选), DuckDB |
| IM 渠道 | 飞书、Slack、Telegram、微信、企微、钉钉、Discord |

---

## 3. 核心智能体架构

### 3.1 Lead Agent（主智能体）

**入口**：`deerflow/agents/lead_agent/agent.py` → `make_lead_agent(config: RunnableConfig)`

```
make_lead_agent(config)
  │
  ├── create_chat_model()          → 构建 LLM（支持 thinking/vision）
  ├── get_available_tools()        → 组装工具集
  ├── apply_prompt_template()      → 生成系统提示词（注入技能、记忆、子智能体说明）
  ├── _build_middlewares()         → 组装中间件链（14-18个）
  │
  └── create_agent(model, tools, middleware, system_prompt, state_schema=ThreadState)
      → CompiledStateGraph (LangGraph 编译图)
```

**关键特性**：
- 动态模型选择，支持 thinking mode 和 vision
- Bootstrap 模式：仅加载 `setup_agent` 工具，用于初始自定义智能体创建
- 自定义智能体模式：额外加载 `update_agent` 工具 + 独立记忆存储

### 3.2 ThreadState（线程状态）

```python
class ThreadState(AgentState):  # 扩展 LangGraph AgentState
    sandbox: NotRequired[SandboxState | None]        # 当前沙箱分配
    thread_data: NotRequired[ThreadDataState | None] # 工作区路径
    title: NotRequired[str | None]                   # 自动生成的对话标题
    artifacts: Annotated[list[str], merge_artifacts] # 去重产物列表（自定义 reducer）
    todos: NotRequired[list | None]                  # 待办/计划项
    uploaded_files: NotRequired[list[dict] | None]   # 上传文件元数据
    viewed_images: Annotated[dict, merge_viewed_images] # 图片缓存（自定义 reducer）
```

自定义 Reducer 语义：
- `merge_artifacts`：去重合并，避免并行分支重复
- `merge_viewed_images`：空 dict 清空整个存储（用于释放视觉上下文）

### 3.3 RuntimeFeatures（特性标志系统）

```python
@dataclass
class RuntimeFeatures:
    sandbox: bool | AgentMiddleware = True       # 沙箱
    memory: bool | AgentMiddleware = False       # 记忆
    summarization: Literal[False] | AgentMiddleware = False  # 摘要
    subagent: bool | AgentMiddleware = False     # 子智能体
    vision: bool | AgentMiddleware = False       # 视觉
    auto_title: bool | AgentMiddleware = False   # 自动标题
    guardrail: Literal[False] | AgentMiddleware = False  # 护栏
```

每个标志三值语义：`False`（禁用）/ `True`（默认内置）/ `AgentMiddleware` 实例（自定义替换）。

---

## 4. 中间件链（核心架构模式）

中间件链是 DeerFlow 最核心的架构模式。14-18 个中间件按严格顺序组装，处理所有横切关注点。

### 4.1 完整中间件链

```
 0  ThreadDataMiddleware          创建线程目录（用户隔离）
 1  UploadsMiddleware             跟踪/注入上传文件
 2  SandboxMiddleware             获取/释放沙箱
 3  DanglingToolCallMiddleware    修复中断的工具调用（补 ToolMessage）
 4  LLMErrorHandlingMiddleware    模型错误恢复
 5  GuardrailMiddleware           工具调用授权（可插拔 GuardrailProvider）
 6  SandboxAuditMiddleware        沙箱命令安全审计
 7  ToolErrorHandlingMiddleware   工具异常 → 错误 ToolMessage
 8  SummarizationMiddleware       上下文压缩（保留技能内容）
 9  TodoMiddleware                计划模式待办跟踪
10  TokenUsageMiddleware          Token 用量记录
11  TitleMiddleware               自动生成对话标题
12  MemoryMiddleware              异步记忆更新队列
13  ViewImageMiddleware           注入 base64 图片数据（视觉）
14  DeferredToolFilterMiddleware  隐藏延迟加载工具的 schema
15  SubagentLimitMiddleware       限制并发子智能体数
16  LoopDetectionMiddleware       检测重复工具调用循环
17  ClarificationMiddleware       拦截澄清请求 → 中断执行（必须最后）
```

### 4.2 关键中间件详解

#### SandboxAuditMiddleware — 命令安全审计

- **范围**：仅拦截 `bash` 工具调用
- **两阶段分类**：
  1. 整命令扫描：高风险模式（`rm -rf /`、`curl|sh`、fork bomb、`LD_PRELOAD`）
  2. 子命令逐个分类：按 `&&`、`||`、`;` 分割后逐个评估
- **风险等级**：`block`（阻止）/ `warn`（警告追加）/ `pass`（放行）
- **输入校验**：空命令、>10000 字符、null 字节 → 拒绝
- **引用感知分割**：未闭合引号 → 视为可疑整体

#### LoopDetectionMiddleware — 循环检测

- **双层检测**：
  1. **哈希检测**：滑动窗口内相同工具调用哈希 ≥3 次 → 警告，≥5 次 → 强制停止
  2. **频率检测**：单工具累计调用 ≥30 次 → 警告，≥50 次 → 强制停止
- **稳定键派生**：工具特定的归一化（如 `read_file` 行范围按 200 分桶）
- **线程安全**：`threading.Lock` + LRU 逐出（最多 100 线程）
- **强制停止**：剥离 `tool_calls`，设置 `finish_reason="stop"` → 强制文本回答

#### SummarizationMiddleware — 上下文压缩

- **技能救援**：识别"技能包"（AIMessage + 配套 ToolMessage），保留最近的 N 个，防止压缩丢失刚加载的技能内容
- **BeforeSummarizationHook**：压缩前触发钩子（如 `memory_flush_hook`），让其他子系统在被删除前捕获数据
- **摘要消息命名**：`HumanMessage(name="summary")`，前端可隐藏但模型仍可见

#### MemoryMiddleware — 记忆更新

- **Hook**：`after_agent`（智能体执行后）
- **流程**：过滤消息 → 检测纠正/强化信号 → 入队 `MemoryUpdateQueue`（带防抖）
- **用户 ID 捕获**：入队时捕获（`threading.Timer` 触发时 ContextVar 不可用）

#### ClarificationMiddleware — 澄清中断

- **Hook**：`wrap_tool_call`，仅拦截 `ask_clarification`
- **行为**：返回 `Command(update={...}, goto=END)` 中断执行，前端展示澄清提示
- **稳定消息 ID**：`clarification:{tool_call_id}` 或 SHA-256，重试调用替换而非追加

#### GuardrailMiddleware — 护栏

- **可插拔 Provider**：`GuardrailProvider` Protocol，`evaluate`/`aevaluate` 方法
- **Fail-closed**：Provider 异常 → 默认阻止调用

---

## 5. 工具系统

### 5.1 工具组装流程

```
get_available_tools(groups, include_mcp, model_name, subagent_enabled, app_config)
  │
  ├── 1. Config 工具        → resolve_variable(cfg.use, BaseTool) 动态加载
  ├── 2. 内置工具           → present_file, ask_clarification, [view_image], [task], [skill_manage]
  ├── 3. MCP 工具           → get_cached_mcp_tools()（懒初始化 + mtime 缓存）
  ├── 4. 社区工具           → tavily, jina_ai, firecrawl, ddg_search, image_search, infoquest, serper
  ├── 5. ACP 工具           → invoke_acp_agent（外部 ACP 兼容智能体）
  │
  └── 去重（按名称优先级）：Config > 内置 > MCP > ACP
```

### 5.2 内置工具清单

| 工具 | 用途 |
|------|------|
| `bash` | 沙箱内执行命令 |
| `ls` | 列出目录 |
| `read_file` | 读取文件 |
| `write_file` | 写入文件 |
| `str_replace` | 精确字符串替换（编辑） |
| `task` | 委派给子智能体 |
| `present_file` | 使输出文件对用户可见 |
| `ask_clarification` | 请求用户澄清（被 ClarificationMiddleware 拦截） |
| `view_image` | 读取图片为 base64（仅视觉模型） |
| `setup_agent` | 引导模式：持久化自定义智能体 SOUL.md + 配置 |
| `update_agent` | 自定义智能体：从内部持久化自更新 |
| `tool_search` | 搜索并加载延迟注册的 MCP 工具 |

### 5.3 工具搜索（延迟加载）

当 `tool_search.enabled` 时：
- MCP 工具注册到 `DeferredToolRegistry`（不暴露 schema）
- `tool_search` 工具按需搜索并加载，保持上下文窗口精简

---

## 6. 子智能体系统

### 6.1 架构

```
task_tool(task, subagent_type, max_turns)
  │
  └── SubagentExecutor.execute_async(task, task_id)
        │
        ├── _create_agent()  → 构建子智能体（build_subagent_runtime_middlewares）
        ├── _filter_tools()  → 应用 allowlist/denylist
        │
        └── _submit_to_isolated_loop_in_context()
              │
              └── agent.astream()  → 在隔离事件循环中执行
                    │
                    ├── 协作式取消检查（cancel_event）
                    ├── SSE 事件发射
                    └── 结果 → SubagentResult
```

### 6.2 关键设计

| 特性 | 实现 |
|------|------|
| **隔离事件循环** | 专用 daemon 线程运行长生命周期 `asyncio.EventLoop`，避免与父智能体冲突 |
| **ContextVar 传播** | `copy_context()` 捕获父上下文，提交到隔离循环 |
| **协作式取消** | `cancel_event: threading.Event`，在 `astream()` 迭代边界检查 |
| **线程池调度** | `ThreadPoolExecutor(max_workers=3)` |
| **并发限制** | `MAX_CONCURRENT_SUBAGENTS = 3`，由 `SubagentLimitMiddleware` 强制 |
| **超时** | 默认 15 分钟 |
| **状态继承** | 子智能体继承父的 `sandbox_state`、`thread_data`、`thread_id`、工具组 |
| **不可嵌套** | 子智能体上下文中禁用 `task` 工具 |
| **状态生命周期** | PENDING → RUNNING → COMPLETED/FAILED/CANCELLED/TIMED_OUT |

### 6.3 内置子智能体

| 子智能体 | 工具集 | 用途 |
|---------|--------|------|
| `general_purpose` | 除 `task` 外所有工具 | 通用任务 |
| `bash_agent` | 仅 `bash` | 命令行专家 |

---

## 7. 记忆系统

### 7.1 架构

```
┌──────────────────────────────────────────────────────┐
│                  Memory Pipeline                       │
│                                                        │
│  MemoryMiddleware.after_agent()                        │
│    → filter_messages_for_memory()                      │
│    → detect_correction() / detect_reinforcement()      │
│    → MemoryUpdateQueue.add()  ──────────┐              │
│                                         │              │
│  SummarizationMiddleware.before_model() │              │
│    → memory_flush_hook()                │              │
│    → MemoryUpdateQueue.add_nowait()  ───┤              │
│                                         ▼              │
│                              MemoryUpdateQueue         │
│                              (防抖 Timer)              │
│                                         │              │
│                                         ▼              │
│                              MemoryUpdater             │
│                              (LLM 摘要对话)            │
│                                         │              │
│                                         ▼              │
│                              MemoryStorage             │
│                              (持久化到 JSON 文件)       │
└──────────────────────────────────────────────────────┘
```

### 7.2 组件详解

| 组件 | 文件 | 职责 |
|------|------|------|
| **MemoryUpdateQueue** | `queue.py` | 线程安全队列 + 防抖，同 thread_id 合并（最新胜出） |
| **MemoryStorage** | `storage.py` | 抽象基类 + 文件实现，per-user/per-agent 作用域 |
| **MemoryUpdater** | `updater.py` | CRUD 操作 + LLM 驱动的记忆摘要更新 |
| **memory_flush_hook** | `summarization_hook.py` | `BeforeSummarizationHook`，压缩前冲刷消息到记忆队列 |
| **MessageProcessing** | `message_processing.py` | 消息过滤 + 纠正/强化信号检测 |

### 7.3 记忆数据结构

```json
{
  "version": "...",
  "user": {
    "workContext": "...",
    "personalContext": "...",
    "topOfMind": "..."
  },
  "history": {
    "recentMonths": "...",
    "earlierContext": "...",
    "longTermBackground": "..."
  },
  "facts": [
    { "id": "...", "content": "...", "category": "...", "confidence": "..." }
  ]
}
```

---

## 8. 沙箱系统

### 8.1 接口设计

```python
class Sandbox(ABC):
    execute_command(command: str) -> str           # 执行命令
    read_file(path: str) -> str                   # 读取文件
    write_file(path: str, content: str, append)   # 写入文件
    list_dir(path: str, max_depth=2) -> list[str] # 列出目录
    glob(path, pattern, ...) -> tuple[list, bool] # 模式匹配
    grep(path, pattern, ...) -> tuple[list, bool] # 内容搜索
    update_file(path: str, content: bytes)         # 更新文件

class SandboxProvider(ABC):
    acquire(thread_id: str) -> str                # 获取沙箱 → sandbox_id
    release(sandbox_id: str)                      # 释放沙箱
    get_sandbox(sandbox_id: str) -> Sandbox       # 获取沙箱实例
    shutdown()                                    # 关闭清理
```

### 8.2 实现与虚拟路径

| Provider | 实现 | 隔离级别 |
|----------|------|---------|
| `LocalSandboxProvider` | 本地文件系统 + subprocess | 线程级（per-thread 沙箱实例） |
| `AioSandboxProvider` | Docker 容器 | 容器级（强隔离） |

**虚拟路径映射**：

```
智能体视角              物理路径
/mnt/user-data/    → .deer-flow/users/{user_id}/threads/{thread_id}/
/mnt/skills/        → skills/{public,custom}/
```

### 8.3 SandboxMiddleware 生命周期

- **懒初始化**（默认）：延迟到首次工具调用时获取沙箱
- **急切初始化**：`before_agent` 阶段获取
- **释放**：`after_agent` 阶段
- **清理**：应用关闭时 `SandboxProvider.shutdown()`

---

## 9. MCP 集成

### 9.1 架构

```
extensions_config.json
  │
  ├── build_servers_config()  → MultiServerMCPClient 配置
  │     │
  │     ├── stdio  → command + args + env
  │     ├── sse    → url + headers
  │     └── http   → url + headers + OAuth
  │
  ├── MultiServerMCPClient  → 连接 MCP 服务器
  │
  └── get_cached_mcp_tools() → 缓存工具列表（mtime 失效）
```

### 9.2 特性

| 特性 | 实现 |
|------|------|
| **传输协议** | stdio、SSE、HTTP |
| **OAuth** | `client_credentials` + `refresh_token` 流程 |
| **缓存** | mtime 失效，启动时初始化 |
| **热重载** | 每次调用 `get_available_tools()` 重读 `extensions_config.json` |
| **延迟加载** | `tool_search` 启用时，MCP 工具注册到 `DeferredToolRegistry` |
| **运行时更新** | Gateway API `PUT /api/mcp` 保存到 `extensions_config.json` |

---

## 10. 技能系统

### 10.1 技能格式

每个技能是一个目录，包含 `SKILL.md`（YAML frontmatter）：

```yaml
---
name: my-skill
description: 技能描述
license: MIT
allowed-tools: [bash, read_file, write_file]
---
技能内容（Markdown）
```

### 10.2 加载流程

```
skills/{public,custom}/
  │
  ├── load_skills()  → 递归扫描 SKILL.md
  │     │
  │     ├── 解析 YAML frontmatter
  │     ├── 读取 extensions_config.json 启用状态
  │     ├── 安全扫描（security_scanner.py）
  │     └── 验证（validation.py）
  │
  └── 注入到智能体系统提示词（容器路径）
```

### 10.3 特性

- **渐进加载**：仅在任务需要时加载，保持上下文窗口精简
- **安装**：`POST /api/skills/install` 提取 `.skill` ZIP 包
- **自定义技能**：`skills/custom/` 目录（gitignored），支持编辑/回滚/历史
- **公共技能**：`skills/public/` 目录（已提交）

---

## 11. 模型工厂

```python
create_chat_model(name, thinking_enabled, app_config, **kwargs) -> BaseChatModel
```

### 解析流程

```
1. 查找 ModelConfig（按名称，默认取配置第一个模型）
2. resolve_class(model_config.use, BaseChatModel) → 动态导入
3. 合并模型设置（排除元数据字段）
4. Thinking 模式处理：
   ├── 启用 → 合并 when_thinking_enabled
   ├── 禁用 → 合并 when_thinking_disabled 或计算禁用设置
   └── Provider 特殊处理：
       ├── OpenAI  → extra_body
       ├── vLLM    → chat_template_kwargs
       ├── Anthropic → native thinking
       ├── Codex   → reasoning_effort
       └── MindIE  → 保守重试默认值
5. 附加追踪回调
```

### Provider 补丁

| Provider | 文件 | 特殊处理 |
|----------|------|---------|
| OpenAI | `patched_openai.py` | 兼容性补丁 |
| DeepSeek | `patched_deepseek.py` | thinking 模式 |
| MiniMax | `patched_minimax.py` | 特定参数 |
| Claude | `claude_provider.py` | OAuth 认证 |
| Codex | `openai_codex_provider.py` | CLI 集成 |
| vLLM | `vllm_provider.py` | 自部署推理 |
| MindIE | `mindie_provider.py` | 华为昇腾推理 |

---

## 12. 配置系统

### 12.1 AppConfig 结构

```python
class AppConfig(BaseModel):
    log_level: str
    models: list[ModelConfig]              # 模型列表
    sandbox: SandboxConfig                 # 沙箱配置
    tools: list[ToolConfig]                # 工具列表
    tool_groups: list[ToolGroupConfig]     # 工具分组
    skills: SkillsConfig                   # 技能配置
    extensions: ExtensionsConfig           # 扩展配置（MCP + 技能状态）
    tool_search: ToolSearchConfig          # 工具搜索
    title: TitleConfig                     # 自动标题
    summarization: SummarizationConfig     # 摘要压缩
    memory: MemoryConfig                   # 记忆
    subagents: SubagentsAppConfig          # 子智能体
    guardrails: GuardrailsConfig           # 护栏
    circuit_breaker: CircuitBreakerConfig  # 熔断器
    database: DatabaseConfig               # 数据库
    checkpointer: CheckpointerConfig       # 检查点
    stream_bridge: StreamBridgeConfig      # 流桥
    run_events: RunEventsConfig            # 运行事件
    acp_agents: dict[str, ACPAgentConfig]  # ACP 外部智能体
    agents_api: AgentsApiConfig            # 智能体 API
    skill_evolution: SkillEvolutionConfig  # 技能进化
    token_usage: TokenUsageConfig          # Token 用量
    tracing: TracingConfig                 # 追踪
```

### 12.2 配置解析链

```
config.yaml (YAML)
  │
  ├── resolve_env_variables()  → $ENV_VAR 替换
  ├── merge extensions_config  → MCP 服务器 + 技能启用状态
  ├── validate ACP agents      → 验证外部智能体配置
  ├── apply singleton configs  → 单例应用
  │
  └── AppConfig 实例
```

### 12.3 单例管理

- `get_app_config()` / `set_app_config()` / `reset_app_config()` — 模块级单例 + mtime 自动重载
- `push_current_app_config()` / `pop_current_app_config()` — ContextVar 栈，运行时作用域覆盖（子智能体可能需要不同配置）

---

## 13. 运行时与流式传输

### 13.1 运行管理

```
HTTP POST /api/threads/{id}/runs/stream
  │
  ├── RunManager.create_or_reject()  → 创建 RunRecord
  ├── asyncio.Task(run_agent())      → 启动智能体执行
  │     │
  │     ├── LangGraph agent.astream()
  │     ├── StreamBridge.publish()   → 发射 SSE 事件
  │     └── StreamBridge.publish_end()
  │
  └── StreamingResponse(sse_consumer())
        ├── StreamBridge.subscribe()  → 订阅事件流
        ├── format_sse()             → 格式化 SSE 帧
        ├── heartbeat                → 空闲心跳
        └── on_disconnect            → 取消/继续策略
```

### 13.2 StreamBridge

| 操作 | 描述 |
|------|------|
| `publish(run_id, event, data)` | 生产者：入队事件 |
| `publish_end(run_id)` | 发送终止信号 |
| `subscribe(run_id, last_event_id)` | 消费者：异步迭代器，支持 `Last-Event-ID` 重连 |
| `cleanup(run_id, delay)` | 延迟释放资源 |

实现：`MemoryStreamBridge` — per-run `asyncio.Queue` + 单调递增事件 ID + 缓冲重放。

---

## 14. IM 渠道集成

### 14.1 架构

```
┌────────────┐     ┌────────────┐     ┌────────────┐
│  Feishu    │     │   Slack    │     │  Telegram  │  ...（7个渠道）
│  Channel   │     │  Channel   │     │  Channel   │
└─────┬──────┘     └─────┬──────┘     └─────┬──────┘
      │ inbound          │ inbound          │ inbound
      ▼                  ▼                  ▼
┌──────────────────────────────────────────────────┐
│                  MessageBus                       │
│  inbound: asyncio.Queue[InboundMessage]           │
│  outbound: list[OutboundCallback]                 │
└──────────────────────┬───────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────┐
│               ChannelManager                      │
│  _dispatch_loop() → 消费 inbound                  │
│  ├── 查找/创建 DeerFlow thread (ChannelStore)     │
│  ├── 流式渠道 → client.runs.stream()              │
│  ├── 非流式渠道 → client.runs.wait()              │
│  └── 并发限制（Semaphore, max=5）                 │
└──────────────────────────────────────────────────┘
```

### 14.2 渠道特性

| 渠道 | 协议 | 流式 | 特殊处理 |
|------|------|------|---------|
| 飞书/Lark | WebSocket | ✓ | 增量更新 ≥0.35s 间隔 |
| Slack | Socket Mode | ✗ | 等待完整响应 |
| Telegram | Polling | ✗ | 轮询消息 |
| 微信 | HTTP Webhook | ✗ | 回调验证 |
| 企微 | HTTP Webhook | ✓ | 增量更新 |
| 钉钉 | HTTP Webhook | ✗ | 回调签名 |
| Discord | discord.py Gateway | ✗ | Bot 框架集成 |

---

## 15. 用户隔离与安全

### 15.1 四层隔离

| 层级 | 机制 | 实现 |
|------|------|------|
| **L1: ContextVar 身份** | 请求级用户 ID | `deerflow_current_user` ContextVar，auth 中间件设置 |
| **L2: Auth + 授权** | JWT + 权限装饰器 | `@require_permission("resource", "action", owner_check=True)` |
| **L3: 文件系统隔离** | per-user 目录 | `.deer-flow/users/{user_id}/threads/{thread_id}/` |
| **L4: 孤儿迁移** | 启动时 | 无主线程 → 分配给管理员 |

### 15.2 权限模型

- `threads:read/write/delete`
- `runs:create/read/cancel`
- Owner check：不同 `user_id` → 404；遗留无主线程 → 读允许，写拒绝

### 15.3 沙箱审计

- 高风险命令阻止（`rm -rf /`、`curl|sh`、fork bomb）
- 中风险命令警告（`pip install`、`chmod 777`、`sudo`）
- 输入校验（空命令、超长、null 字节）
- 路径遍历保护（产物路径验证）

---

## 16. Gateway API 表面

| 模块 | 路径前缀 | 关键端点 |
|------|---------|---------|
| Models | `/api/models` | 列表/详情 |
| MCP | `/api/mcp` | 获取/更新配置 |
| Memory | `/api/memory` | CRUD + 搜索 + 命名空间 |
| Skills | `/api/skills` | 列表/切换/自定义 CRUD/安装 |
| Artifacts | `/api/threads/{id}/artifacts` | 下载产物 |
| Uploads | `/api/threads/{id}/uploads` | 上传/列表/删除 |
| Threads | `/api/threads` | CRUD/搜索/状态/历史 |
| Agents | `/api/agents` | 列表/创建/配置/删除 |
| Runs | `/api/threads/{id}/runs` | 创建/流式/等待/取消/消息 |
| Auth | `/api/v1/auth` | 登录/注册/OAuth/JWT |
| Channels | `/api/channels` | 状态/重启 |
| Feedback | `/api/threads/{id}/runs/{id}/feedback` | CRUD/统计 |
| Suggestions | `/api/threads/{id}/suggestions` | 生成后续建议 |

---

## 17. 关键设计模式总结

| 模式 | 应用位置 | 描述 |
|------|---------|------|
| **中间件链** | 智能体执行 | 14-18 个中间件严格顺序，处理所有横切关注点 |
| **抽象工厂 + 策略** | 沙箱 | `SandboxProvider` 工厂 + `Sandbox` 策略，可替换实现 |
| **特性标志组合** | 智能体工厂 | 三值标志（禁用/默认/自定义）声明式控制中间件 |
| **观察者/发布-订阅** | IM 渠道 | MessageBus 解耦渠道与调度器 |
| **生产者-消费者桥** | 流式传输 | StreamBridge 连接智能体执行与 SSE 消费 |
| **线程池 + 隔离循环** | 子智能体 | 持久事件循环 + 协作式取消 + ContextVar 传播 |
| **防抖队列** | 记忆 | Timer 防抖 + 同线程合并，避免频繁 LLM 调用 |
| **懒初始化 + 缓存** | MCP 工具 | 启动时初始化，mtime 失效，运行时热重载 |
| **分层配置 + ContextVar 栈** | 配置 | 文件单例 + mtime 重载 + 运行时 ContextVar 覆盖 |
| **动态类加载** | 模型/工具 | `resolve_class`/`resolve_variable` 按点路径导入 |
| **安全门控加载** | 技能 | 解析 → 验证 → 安全扫描 → 启用 |
| **Harness/App 边界** | 项目结构 | 单向依赖，CI 强制执行 |
| **虚拟文件系统** | 沙箱 | 智能体视角 `/mnt/` → 物理路径映射 |
| **循环检测** | 中间件 | 哈希 + 频率双层检测，强制文本回答打破循环 |
| **Fail-closed** | 护栏 | Provider 异常 → 默认阻止，安全优先 |

---

## 18. 数据流全景

```
用户请求 (HTTP / IM)
  │
  ├── Auth 中间件 → 设置 ContextVar(user_id)
  ├── 权限检查 → @require_permission
  │
  ▼
RunManager.create_or_reject()
  │
  ▼
run_agent() [asyncio.Task]
  │
  ├── make_lead_agent(config)
  │     ├── create_chat_model() → LLM
  │     ├── get_available_tools() → Tools
  │     ├── _build_middlewares() → Middleware Chain
  │     └── apply_prompt_template() → System Prompt
  │
  ▼
LangGraph agent.astream(input, config)
  │
  ├── [Middleware Chain 执行]
  │     ├── before_agent → ThreadData, Sandbox 获取
  │     ├── before_model → Summarization, ViewImage
  │     ├── LLM 调用 → 模型推理
  │     ├── after_model → LoopDetection, Title
  │     ├── wrap_tool_call → Guardrail, Audit, ErrorHandling, Clarification
  │     ├── 工具执行 → bash/read_file/write_file/task/...
  │     └── after_agent → Memory 入队, Sandbox 释放
  │
  ├── StreamBridge.publish() → SSE 事件
  │
  └── StreamBridge.publish_end()

SSE Consumer → format_sse() → HTTP Response (text/event-stream)
```
