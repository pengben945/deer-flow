# 子智能体系统 — 并行编排与隔离执行

> DeerFlow Agent Harness 深度分析 · 第 3 篇

---

## 1. 概述与定位

### 在整体架构中的位置

子智能体系统是 DeerFlow 实现"超级智能体"（Super Agent）的核心机制——Lead Agent 作为编排者，通过 `task` 工具将子任务委派给专业化的子智能体，实现并行执行、专业分工和上下文隔离。

```
┌──────────────────────────────────────────────────────────┐
│                     Lead Agent (编排者)                    │
│                                                           │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐              │
│  │ task    │    │ task    │    │ task    │              │
│  │ tool    │    │ tool    │    │ tool    │              │
│  └────┬────┘    └────┬────┘    └────┬────┘              │
│       │              │              │                    │
│  ┌────▼────┐    ┌────▼────┐    ┌────▼────┐              │
│  │Subagent │    │Subagent │    │Subagent │              │
│  │Executor │    │Executor │    │Executor │              │
│  └────┬────┘    └────┬────┘    └────┬────┘              │
│       │              │              │                    │
│  ┌────▼────┐    ┌────▼────┐    ┌────▼────┐              │
│  │general- │    │  bash   │    │ custom  │              │
│  │purpose  │    │ agent   │    │ agent   │              │
│  └─────────┘    └─────────┘    └─────────┘              │
└──────────────────────────────────────────────────────────┘
```

### 解决的核心问题

1. **专业分工**：不同任务需要不同的工具集和提示词（bash 专家 vs 通用智能体）
2. **上下文隔离**：子智能体有自己的对话历史，不污染父上下文
3. **并行执行**：多个子智能体可以同时运行，提高效率
4. **资源限制**：防止无限委派（嵌套深度、调用次数、超时）
5. **状态继承**：子智能体继承父的沙箱、线程数据，确保文件系统一致性

### 一句话设计哲学

**"子智能体是 Lead Agent 的延伸，不是替代——它继承环境、专注任务、汇报结果。"**

---

## 2. 架构总览

### 2.1 核心组件

```
SubagentConfig (配置)          SubagentRegistry (注册表)
       │                              │
       ▼                              ▼
SubagentExecutor (执行器)  ←──  subagent_registry.get(name)
       │
       ├── create_react_agent()  → 构建子智能体
       ├── _build_tools()        → 过滤工具集
       ├── _build_system_message() → 构建系统提示词
       └── agent.ainvoke()       → 执行推理循环
```

### 2.2 完整执行流程

```
Lead Agent 调用 task(description, subagent_type)
  │
  ├── 1. subagent_registry.get(subagent_type) → SubagentConfig
  ├── 2. SubagentLimitMiddleware.check_allowed() → 检查限制
  ├── 3. SubagentLimitMiddleware.record_invocation() → 记录调用
  │
  └── 4. SubagentExecutor(config).execute(description, messages, config)
        │
        ├── _build_tools() → 过滤工具集
        ├── _build_system_message() → 构建系统提示词
        ├── _get_model() → 解析模型（支持 "inherit"）
        │
        ├── create_react_agent(model, tools, state_modifier)
        │
        └── agent.ainvoke({"messages": [system + history + task]})
              │
              ├── 推理循环（think → act → observe → ...）
              │
              └── 返回 {result, turns, duration_seconds, subagent_name, model_used}
```

---

## 3. 源码走读

### 3.1 SubagentConfig — 子智能体配置

```python
@dataclass
class SubagentConfig:
    name: str                          # 唯一标识
    description: str                   # 何时使用的描述
    system_prompt: str                 # 系统提示词
    tools: list[str] | None = None     # 工具列表（None = 继承全部）
    disallowed_tools: list[str] = field(default_factory=list)  # 禁用工具
    model: str = "inherit"             # 模型（"inherit" = 继承父模型）
    max_turns: int = 50                # 最大轮次
    prompt_template: str | None = None # 任务模板
    state_modifier: Any = None         # 状态修改器
```

**关键设计**：
- `tools=None` 表示继承父智能体的全部工具，`tools=["bash"]` 表示仅使用指定工具
- `disallowed_tools` 始终包含 `["task", "ask_clarification", "present_files"]`，防止嵌套和澄清循环
- `model="inherit"` 让子智能体使用与父相同的模型，避免配置冗余

### 3.2 内置子智能体

#### bash 子智能体

```python
BASH_AGENT_CONFIG = SubagentConfig(
    name="bash",
    description="A specialized agent for executing shell commands...",
    system_prompt="""You are a bash execution subagent...
    <guidelines>
    - Execute commands precisely as specified
    - Report command output verbatim when short, summarize when long
    - If a command fails, report the error and exit code clearly
    - Do NOT attempt to fix or retry failed commands unless explicitly asked
    </guidelines>
    """,
    tools=["bash"],                          # 仅 bash 工具
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=30,                            # 最多 30 轮
)
```

**设计动机**：bash 子智能体是"命令执行专家"——它只做一件事（执行命令），但做得很好。限制工具集防止它偏离职责（如自己搜索网页、修改文件）。

#### general-purpose 子智能体

```python
GENERAL_PURPOSE_CONFIG = SubagentConfig(
    name="general-purpose",
    description="A capable agent for complex, multi-step tasks...",
    system_prompt="""You are a general-purpose subagent...
    <guidelines>
    - Focus on completing the delegated task efficiently
    - Think step by step but act decisively
    - Do NOT ask for clarification - work with the information provided
    </guidelines>
    <working_directory>
    - User uploads: /mnt/user-data/uploads
    - User workspace: /mnt/user-data/workspace
    - Output files: /mnt/user-data/outputs
    </working_directory>
    """,
    tools=None,                              # 继承全部工具
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=100,                           # 最多 100 轮
)
```

**关键区别**：
- `tools=None`：继承父的全部工具，适合复杂多步任务
- `max_turns=100`：允许更多轮次，因为通用任务可能需要多步推理
- 提示词中明确工作目录结构，确保文件操作路径正确

### 3.3 SubagentRegistry — 子智能体注册表

```python
class SubagentRegistry:
    def __init__(self) -> None:
        self._subagents: dict[str, SubagentConfig] = {}

    def register(self, config: SubagentConfig) -> None:
        if config.name in self._subagents:
            raise ValueError(
                f"Subagent '{config.name}' is already registered. "
                f"Use unregister() first if you want to replace it."
            )
        self._subagents[config.name] = config

    def get(self, name: str) -> SubagentConfig | None:
        return self._subagents.get(name)

    def all(self) -> list[SubagentConfig]:
        return list(self._subagents.values())

    def names(self) -> list[str]:
        return list(self._subagents.keys())

# 模块级单例
subagent_registry = SubagentRegistry()

def register_builtins() -> None:
    from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
    for config in BUILTIN_SUBAGENTS:
        subagent_registry.register(config)
```

**设计模式**：模块级单例 + 显式注册。`register()` 拒绝重复注册（fail-fast），`unregister()` 支持运行时替换。

### 3.4 SubagentExecutor — 子智能体执行器

```python
class SubagentExecutor:
    def __init__(self, config: SubagentConfig):
        self.config = config
        self._turn_count = 0
        self._start_time: datetime | None = None
        self._end_time: datetime | None = None

    async def execute(
        self,
        task_description: str,
        conversation_history: list[BaseMessage],
        runnable_config: RunnableConfig,
    ) -> dict[str, Any]:
        self._start_time = datetime.now(timezone.utc)
        self._turn_count = 0

        try:
            # 1. 构建工具集
            tools = self._build_tools()

            # 2. 构建系统提示词
            system_message = self._build_system_message(task_description)

            # 3. 构建初始消息
            messages = [system_message] + conversation_history + [
                HumanMessage(content=f"[Delegated Task]\n{task_description}")
            ]

            # 4. 创建并运行 ReAct 智能体
            agent = create_react_agent(
                model=self._get_model(runnable_config),
                tools=tools,
                state_modifier=self.config.state_modifier,
            )

            result = await agent.ainvoke(
                {"messages": messages},
                config=runnable_config,
            )

            # 5. 提取最终 AI 消息
            final_message = result["messages"][-1] if result.get("messages") else None
            response_text = (
                final_message.content
                if isinstance(final_message, AIMessage)
                else str(final_message)
            )

            self._turn_count = len(result.get("messages", []))

            return {
                "result": response_text,
                "turns": self._turn_count,
                "duration_seconds": self._compute_duration(),
                "subagent_name": self.config.name,
                "model_used": self._get_model_name(runnable_config),
            }

        except Exception as e:
            logger.exception("Subagent %s execution failed", self.config.name)
            return {
                "result": f"Subagent execution failed: {e}",
                "turns": self._turn_count,
                "duration_seconds": self._compute_duration(),
                "subagent_name": self.config.name,
                "model_used": self._get_model_name(runnable_config),
                "error": True,
            }

        finally:
            self._end_time = datetime.now(timezone.utc)
```

**关键设计决策**：

1. **消息构造**：`[system_message] + conversation_history + [HumanMessage("[Delegated Task]...")]`
   - 系统提示词在最前，定义子智能体的角色
   - 父对话历史在中间，提供上下文
   - 委派任务在最后，明确当前目标

2. **使用 `create_react_agent`**：子智能体是标准的 LangGraph ReAct 智能体，不是自定义图。这确保了与 LangGraph 生态的兼容性。

3. **异常不传播**：`except Exception` 捕获所有异常，返回错误结果而非让异常传播到父智能体。这实现了"子智能体失败不崩溃父智能体"的隔离语义。

### 3.5 工具过滤：`_build_tools()`

```python
def _build_tools(self) -> list[Any]:
    if self.config.tools is None:
        # 继承全部工具
        all_tools = tool_registry.all()
    else:
        # 使用指定工具
        all_tools = []
        for tool_name in self.config.tools:
            tool = tool_registry.get(tool_name)
            if tool is not None:
                all_tools.append(tool)
            else:
                logger.warning(
                    "Subagent %s: tool %r not found in registry, skipping",
                    self.config.name, tool_name,
                )

    # 移除禁用工具
    disallowed = set(self.config.disallowed_tools)
    filtered = [t for t in all_tools if t.name not in disallowed]

    return filtered
```

**两层过滤**：
1. **正向过滤**：`tools=None`（继承全部）或 `tools=["bash"]`（仅指定）
2. **负向过滤**：`disallowed_tools` 始终移除 `task`、`ask_clarification`、`present_files`

**为什么禁用 `task`？** 防止嵌套委派——子智能体不应再委派给子子智能体，否则会导致无限递归和资源耗尽。

**为什么禁用 `ask_clarification`？** 子智能体应自主完成任务，不应向用户请求澄清。如果需要澄清，应返回结果让父智能体决定。

### 3.6 SubagentLimitMiddleware — 调用限制

```python
class SubagentLimitMiddleware:
    def __init__(
        self,
        max_total_invocations: int = 10,   # 总调用上限
        max_per_subagent: int = 5,         # 单类型调用上限
        max_depth: int = 3,                # 嵌套深度上限
    ):
        self.max_total_invocations = max_total_invocations
        self.max_per_subagent = max_per_subagent
        self.max_depth = max_depth
        self._invocation_counts: dict[str, int] = defaultdict(int)
        self._total_invocations = 0
        self._current_depth = 0

    def check_allowed(self, subagent_name: str, depth: int = 0) -> tuple[bool, str]:
        # 检查总调用限制
        if self._total_invocations >= self.max_total_invocations:
            return False, f"Total limit reached ({self._total_invocations}/{self.max_total_invocations})"

        # 检查单类型限制
        if self._invocation_counts[subagent_name] >= self.max_per_subagent:
            return False, f"Per-subagent limit reached for '{subagent_name}'"

        # 检查嵌套深度
        if depth > self.max_depth:
            return False, f"Delegation chain too deep (depth={depth}, max={self.max_depth})"

        return True, ""
```

**三重限制**：

| 限制 | 默认值 | 防御场景 |
|------|--------|---------|
| `max_total_invocations` | 10 | 防止 Agent 无限制地委派任务 |
| `max_per_subagent` | 5 | 防止反复调用同一子智能体（循环委派） |
| `max_depth` | 3 | 防止深层嵌套（虽然 `task` 已禁用，这是额外保险） |

### 3.7 task 工具 — 委派入口

```python
@tool(args_schema=TaskInput)
async def task(
    subagent_type: str,
    description: str,
    *,
    config: RunnableConfig,
    state: Annotated[dict, InjectedState],
) -> str:
    # 1. 查找子智能体配置
    subagent_config = subagent_registry.get(subagent_type)
    if subagent_config is None:
        available = subagent_registry.names()
        return json.dumps({
            "error": True,
            "result": f"Unknown subagent type: '{subagent_type}'. Available: {available}",
            ...
        })

    # 2. 检查调用限制
    depth = state.get("delegation_depth", 0)
    allowed, reason = _limit_middleware.check_allowed(subagent_type, depth)
    if not allowed:
        return json.dumps({"error": True, "result": f"Subagent invocation denied: {reason}", ...})

    # 3. 记录调用
    _limit_middleware.record_invocation(subagent_type, depth)

    # 4. 获取对话历史
    messages = state.get("messages", [])

    # 5. 创建执行器并运行
    executor = SubagentExecutor(subagent_config)
    result = await executor.execute(description, messages, config)

    # 6. 返回 JSON 结果
    return json.dumps(result)
```

---

## 4. 核心机制详解

### 4.1 模型继承：`model="inherit"`

```python
def _get_model(self, runnable_config: RunnableConfig) -> Any:
    if self.config.model == "inherit":
        return runnable_config.get("configurable", {}).get("model", "default")
    return self.config.model
```

**设计动机**：大多数情况下，子智能体应使用与父相同的模型。显式配置 `model="gpt-4o"` 只在需要不同模型时使用（如 bash 子智能体用更便宜的模型）。

### 4.2 状态继承与隔离

子智能体继承父的：
- **对话历史**：`conversation_history` 传入 `execute()`
- **RunnableConfig**：`runnable_config` 传入，包含 `thread_id`、`sandbox_state` 等
- **沙箱环境**：通过 `runnable_config` 中的 `sandbox_id` 继承

子智能体不继承的：
- **工具调用历史**：子智能体有自己的消息流
- **Title / Memory / Todo**：这些 Lead 专属中间件不在子智能体中
- **Clarification**：子智能体不能请求用户澄清

### 4.3 错误隔离

```python
except Exception as e:
    logger.exception("Subagent %s execution failed", self.config.name)
    return {
        "result": f"Subagent execution failed: {e}",
        "error": True,
        ...
    }
```

子智能体的异常不会传播到父智能体。父智能体收到的是一个包含 `error: True` 的结果，可以决定如何处理（重试、忽略、报告给用户）。这是**舱壁模式**（Bulkhead Pattern）的应用——一个子智能体的失败不会拖垮整个系统。

---

## 5. 设计模式提取

### 5.1 编排者-工作者模式（Orchestrator-Worker）

Lead Agent 是编排者，子智能体是工作者。编排者决定"做什么"，工作者决定"怎么做"。

### 5.2 注册表模式（Registry）

`SubagentRegistry` 是经典的注册表模式——按名称查找配置，支持动态注册/注销。

### 5.3 舱壁模式（Bulkhead）

子智能体的异常不传播，资源使用有限制（调用次数、嵌套深度、超时），确保一个子智能体的失败不影响其他。

### 5.4 策略模式（Strategy）

`SubagentConfig.tools` 决定工具策略——`None`（继承全部）vs 显式列表（限定范围）。

### 5.5 模板方法模式（Template Method）

`_build_system_message()` 是模板方法——基类定义骨架（system_prompt + prompt_template），子类可通过 `prompt_template` 定制。

---

## 6. 业界对比

| 特性 | DeerFlow | AutoGen | CrewAI | LangGraph Subgraph |
|------|---------|---------|--------|-------------------|
| **编排模式** | Orchestrator-Worker | Conversational | Sequential/Parallel | Graph composition |
| **子智能体定义** | SubagentConfig dataclass | Agent 类 | Agent 类 | CompiledGraph |
| **工具隔离** | allowlist/denylist | 无限制 | 无限制 | 共享 |
| **嵌套限制** | 三重限制（total/per-type/depth） | 无 | 无 | 无 |
| **模型继承** | `model="inherit"` | 显式配置 | 显式配置 | 共享 |
| **错误隔离** | 异常不传播 | 传播 | 传播 | 传播 |
| **并行执行** | 支持（SubagentLimitMiddleware 限制并发） | 支持 | 支持 | 支持 |
| **状态继承** | 沙箱/线程数据/对话历史 | 消息传递 | 消息传递 | 图状态 |

**DeerFlow 的独特之处**：
1. **三重限制**：其他框架通常不限制子智能体调用次数和嵌套深度
2. **错误隔离**：子智能体异常不传播，父智能体可以优雅处理
3. **工具 denylist**：`disallowed_tools` 提供负向过滤，比 CrewAI 的无限制更安全

---

## 7. 面试关联

### Q1: 多智能体编排有哪些模式？各有什么优缺点？

**标准回答**：

常见模式有：顺序编排、并行编排、编排者-工作者、对话式多智能体、图编排。

**加分项**：

> "DeerFlow 使用**编排者-工作者模式**——Lead Agent 作为编排者决定委派什么任务给哪个子智能体，子智能体自主完成任务后汇报结果。这种模式的优势是：编排者保持全局视野，可以协调多个子智能体的结果；子智能体专注局部任务，提示词和工具集可以定制。劣势是：编排者是单点，如果编排者的委派决策错误（委派给错误的子智能体类型），整个任务会失败。DeerFlow 通过三重限制（总调用上限 10、单类型上限 5、嵌套深度上限 3）防止编排者过度委派，通过错误隔离（子智能体异常不传播）确保一个子智能体的失败不影响其他。"

### Q2: 子智能体的隔离与通信如何设计？

**标准回答**：

子智能体应有独立的上下文，通过消息传递通信。

**加分项**：

> "DeerFlow 的子智能体隔离是**半隔离**——继承环境但独立执行。继承的包括：对话历史（提供上下文）、沙箱环境（确保文件系统一致性）、RunnableConfig（包含 thread_id 等运行时信息）。不继承的包括：工具调用历史（子智能体有自己的消息流）、Lead 专属中间件（Title/Memory/Todo/Clarification）。通信是**单向的**：父→子（通过 task description），子→父（通过返回结果）。没有双向通信或共享状态。这种设计简化了实现，但意味着子智能体不能向父请求额外信息——如果信息不足，只能返回不完整结果让父决定。"

### Q3: 如何防止子智能体无限递归？

**标准回答**：

限制嵌套深度，禁止子智能体调用委派工具。

**加分项**：

> "DeerFlow 用**双重防御**：第一层是工具级——子智能体的 `disallowed_tools` 始终包含 `task`，从工具层面禁止嵌套委派；第二层是中间件级——`SubagentLimitMiddleware` 检查嵌套深度（默认上限 3），即使 `task` 工具被意外暴露，深度限制也会阻止无限递归。这种'纵深防御'思想在安全系统中很常见——一层被绕过，下一层仍然有效。"

---

## 8. 扩展思考

### 8.1 局限与改进方向

| 局限 | 改进方向 |
|------|---------|
| 子智能体不能向父请求额外信息 | 支持双向通信（子→父请求，父→子补充） |
| 子智能体结果仅文本 | 支持结构化结果（JSON schema 验证） |
| 无子智能体级指标 | 记录每个子智能体的 token 用量、延迟、错误率 |
| SubagentLimitMiddleware 非标准 AgentMiddleware | 重构为标准中间件，集成到中间件链 |
| 工具继承是全有或全无 | 支持工具组继承（`tools="group:search"`） |
| 无子智能体重试 | 支持配置重试策略（次数、退避） |

### 8.2 如果重新设计

1. **流式子智能体**：子智能体执行过程中流式返回中间结果，而非等待完成
2. **子智能体组合**：支持管道式组合（subagent_1 → subagent_2 → subagent_3）
3. **动态子智能体**：LLM 可以在运行时创建新的子智能体配置
4. **子智能体市场**：类似 MCP 工具市场，可安装社区子智能体
5. **子智能体调试**：支持单步执行、断点、检查子智能体内部状态

### 8.3 与前沿研究/产品的关联

- **AutoGen**：微软的多智能体框架，支持对话式编排，DeerFlow 的编排者-工作者模式更简单但更可控
- **CrewAI**：基于角色的多智能体，DeerFlow 的 SubagentConfig 类似但更轻量
- **LangGraph Subgraph**：图组合模式，DeerFlow 的子智能体是独立的 ReAct 智能体而非子图
- **OpenAI Swarm**：轻量多智能体编排，DeerFlow 的三重限制和错误隔离更健壮
