# Subagent 执行逻辑深度分析

> 分析时间：2026-05-31
> 分析范围：deer-flow 子代理系统核心代码

---

## 1. 核心文件与模块结构

### 1.1 子代理核心包

| 文件 | 作用 |
|---|---|
| `deerflow/subagents/__init__.py` | 包导出，暴露 `SubagentConfig`、`SubagentExecutor`、`SubagentResult`、`registry` 函数 |
| **`deerflow/subagents/executor.py`** | **核心执行引擎** — `SubagentExecutor`（创建 agent、过滤工具、异步执行）、`SubagentResult`（封装结果）、`SubagentStatus`（状态枚举）、全局后台任务存储、隔离事件循环管理（~800 行） |
| `deerflow/subagents/registry.py` | 代理注册表 — 四层配置解析（内置 → 自定义 → 每代理覆盖 → 最终化）；`get_subagent_config()` 是配置查找的中心枢纽 |
| `deerflow/subagents/config.py` | `SubagentConfig` 数据类：name、description、system_prompt、tools、disallowed_tools、skills、model、max_turns、timeout_seconds |
| `deerflow/subagents/builtins/__init__.py` | 将内置子代理注册到 `BUILTIN_SUBAGENTS` 字典 |
| `deerflow/subagents/builtins/general_purpose.py` | "general-purpose" 子代理（所有工具，去除了 task/ask_clarification/present_files，max_turns=100） |
| `deerflow/subagents/builtins/bash_agent.py` | "bash" 子代理（仅沙箱工具，max_turns=60） |

### 1.2 桥接与编排

| 文件 | 作用 |
|---|---|
| **`deerflow/tools/builtins/task_tool.py`** | **`task` 工具** — 主代理 LLM 调用的入口点。处理配置解析、executor 创建、后台启动、轮询循环、SSE 流式事件、协调取消 |
| `deerflow/agents/middlewares/subagent_limit_middleware.py` | `SubagentLimitMiddleware` — 在 `after_model` 阶段截断单次 LLM 响应中超过 `MAX_CONCURRENT_SUBAGENTS`（默认 3，限制在 [2,4] 范围）的多余 `task` 调用 |
| `deerflow/agents/middlewares/tool_error_handling_middleware.py` | `build_subagent_runtime_middlewares()` — 构建子代理的 LangGraph 中间件链 |

### 1.3 配置与测试

| 文件 | 作用 |
|---|---|
| `deerflow/config/subagents_config.py` | 从 `config.yaml` 加载的 Pydantic 模型：`SubagentsAppConfig`、`SubagentOverrideConfig`（每代理）、`CustomSubagentConfig`（用户定义类型） |
| `tests/test_subagent_executor.py` | 单元测试同步/异步执行路径、错误处理、协作取消 |
| `tests/test_subagent_timeout_config.py` | 超时和配置解析测试 |
| `tests/test_subagent_limit_middleware.py` | SubagentLimitMiddleware 截断逻辑测试 |
| `tests/test_subagent_skills_config.py` | 子代理技能配置测试 |
| `tests/test_subagent_prompt_security.py` | 提示注入安全性测试 |

---

## 2. 完整执行流程（6 阶段）

### 阶段 1：LLM 生成 `task` 工具调用

1. 主代理系统提示中已包含子代理相关部分（`prompt.py` 中的 `_build_subagent_section()`），指导 LLM 如何将复杂任务分解为并行子代理
2. 当 `subagent_enabled=True` 时，`get_available_tools()` 将 `task_tool` 函数添加到主代理的工具列表
3. LLM 决定委派任务，调用 `task` 工具，参数包括：`description`、`prompt`、`subagent_type`、可选的 `max_turns`

### 阶段 2：task_tool 函数（task_tool.py）— 编排中枢

```
输入：description, prompt, subagent_type, [max_turns]
输出：String（"Task Succeeded. Result: ..." 或 "Task failed. Error: ..."）
```

执行步骤：

1. **配置解析**：
   - 调用 `get_subagent_config(subagent_type)` 解析内置/自定义配置 + config.yaml 覆盖
   - 特别检查 `bash` 子代理：若 `is_host_bash_allowed()` 返回 false 则拒绝

2. **上下文提取**：
   - 从 `runtime.state` 提取父代理的 `sandbox`、`thread_data`
   - 从 `runtime.config` / `runtime.context` 提取 `thread_id`、`parent_model`、`trace_id`
   - 通过 `_merge_skill_allowlists()` 合并父级和子级的技能允许列表

3. **工具组装**：
   - 调用 `get_available_tools()` 且 `subagent_enabled=False`（禁止递归嵌套）
   - 继承父级 `tool_groups` 限制
   - 过滤：`SubagentExecutor` 构造函数进一步根据子代理的 `tools`（允许列表）和 `disallowed_tools`（拒绝列表）过滤

4. **Executor 创建**：
   - 实例化 `SubagentExecutor`，传入已解析的配置、过滤后的工具、沙箱状态和父级信息

5. **后台启动**：
   - 调用 `executor.execute_async(prompt, task_id=tool_call_id)` — 用工具调用 ID 实现可追溯性
   - 立即返回 `task_id`，进入轮询

6. **轮询循环**：
   - 每 5 秒通过 `get_background_task_result(task_id)` 检查一次结果
   - 每次有新的 AI 消息可用时触发 SSE 事件（`task_started`、`task_running`、`task_completed`/`task_failed`/`task_timed_out`/`task_cancelled`）
   - 最大轮询次数：`(config.timeout_seconds + 60) / 5`（超时加 60 秒缓冲）
   - 终止时调用 `cleanup_background_task(task_id)` 防止内存泄漏

7. **异步取消处理**：
   - 父协程被取消（`asyncio.CancelledError`）时，通过 `request_cancel_background_task()` 信号通知后台线程停止
   - 启动延迟清理协程，轮询后台任务进入终止状态

### 阶段 3：SubagentExecutor 启动（executor.py）

`execute_async()` 方法：

1. 创建 `SubagentResult`（状态 `PENDING`）
2. 注册到全局 `_background_tasks` 字典（线程安全，带锁保护）
3. 提交到 `_scheduler_pool`（3 个工作线程的 `ThreadPoolExecutor`，线程名 `subagent-scheduler-`）

在后台线程中：
1. 更新状态为 `RUNNING`
2. 通过 `_submit_to_isolated_loop_in_context()` 提交给隔离的持久事件循环
3. 调用 `execution_future.result(timeout=self.config.timeout_seconds)` 等待（带超时）

### 阶段 4：隔离事件循环机制

设计目标：解决"asyncio 在已运行的事件循环中运行"的问题。

- 当同步 API（`execute()`）在已运行的事件循环中被调用时，不能使用 `asyncio.run()`（会抛 `RuntimeError: cannot be called from a running event loop`）
- **解决方案**：每个进程持有一个隔离事件循环，运行在专用守护线程中（`subagent-persistent-loop`）
- 延迟创建：`_get_isolated_subagent_loop()` 首次调用时创建，后续复用
- ContextVar 保护：`_submit_to_isolated_loop_in_context()` 使用 `contextvars.copy_context()` 保留上下文变量状态，通过 `asyncio.run_coroutine_threadsafe()` 提交
- 关闭注册：通过 `atexit` 注册关闭函数，unregister + re-register 模式支持热重载

### 阶段 5：异步核心执行（_aexecute）

**Agent 创建**（`_create_agent()`）：
1. 使用 `resolve_subagent_model_name()` 解析模型名（`"inherit"` → 父级模型 → 默认配置模型）
2. 创建聊天模型（禁用 thinking 以节省 token）
3. 构建子代理中间件链：

```
ThreadDataMiddleware（延迟）
→ SandboxMiddleware（延迟）
→ DanglingToolCallMiddleware
→ LLMErrorHandlingMiddleware
→ GuardrailMiddleware（可选）
→ SandboxAuditMiddleware
→ ToolErrorHandlingMiddleware
→ ViewImageMiddleware（如果模型支持视觉）
```

> **设计选择**：子代理**不含**以下中间件 —— `UploadsMiddleware`（无文件上传）、`SummarizationMiddleware`、`TodoMiddleware`、`TitleMiddleware`、`MemoryMiddleware`、`SubagentLimitMiddleware`、`LoopDetectionMiddleware`、`ClarificationMiddleware`。子代理设计为 **无状态单次任务执行器**，不需要这些功能。

**状态构建**（`_build_initial_state()`）：
1. 调用 `_load_skill_messages()` 从磁盘异步加载子代理已启用的技能（由 `config.skills` 白名单过滤）
2. 除非明确禁用，技能作为 `SystemMessage` 注入初始状态
3. 任务描述作为最终 `HumanMessage` 附加
4. 父代理的 `sandbox_state` 和 `thread_data` 通过参数传递

**Agent 执行**：
1. 使用流式模式（`stream_mode="values"`）调用 `agent.astream()`
2. `recursion_limit` 设为 `config.max_turns`
3. `thread_id` 透传给沙箱访问的配置
4. 每次流迭代中：
   - 检查 `result.cancel_event` 进行协作取消
   - 提取并去重 AI 消息（通过消息 ID 或完整字典比较）

**结果提取**：
- 找到最终 AI 消息
- 处理字符串和列表两种内容类型（内容块 → 文本片段拼接）
- 处理无 AI 消息或空状态的边界情况
- 返回包含状态、结果、错误、时间戳和捕获的 AI 消息的 `SubagentResult`

### 阶段 6：结果返回主代理

1. 轮询循环检测到终止状态
2. 格式化为 `"Task Succeeded. Result: ..."` 或 `"Task failed. Error: ..."`
3. 通过 LangGraph 流式机制作为 `ToolMessage` 呈现给主代理 LLM

---

## 3. 关键组件协作图

```
Lead Agent LLM
      │
      │ 调用 task(description, prompt, subagent_type)
      ▼
task_tool()  [task_tool.py]
      │
      ├── 解析子代理配置 (registry.get_subagent_config)
      ├── 获取可用工具 (get_available_tools)
      ├── 创建 SubagentExecutor
      ├── 调用 executor.execute_async()
      │
      ├── 轮询循环 (每 5 秒)
      │       ├── SSE 事件 (task_started, running, completed)
      │       └── 返回结果给 LLM
      │
      ▼
SubagentExecutor.execute_async()  [executor.py]
      │
      ├── 创建 PENDING 结果，存入 _background_tasks
      └── 提交至 _scheduler_pool (3 线程)
              │
              ▼
      后台线程 [subagent-scheduler-N]
              │
              ├── 状态 → RUNNING
              └── 通过 _submit_to_isolated_loop_in_context() 提交
                      │
                      ▼
                 隔离事件循环 [subagent-persistent-loop 线程]
                      │
                      ▼
                 SubagentExecutor._aexecute()
                      │
                      ├── _create_agent() 构建子代理中间件链
                      ├── _build_initial_state() 注入技能
                      ├── agent.astream() 流式执行
                      ├── 捕获 AI 消息
                      └── 提取最终结果
                      │
                      ▼
                 SubagentResult (COMPLETED/FAILED/TIMED_OUT)
                      │
                      ▼
              _background_tasks[task_id] (全局字典，锁保护)
                      │
                      ├── task_tool 轮询循环读取
                      └── cleanup_background_task() 清除
```

### SubagentLimitMiddleware 的插入位置

```
_build_middlewares() 中的 after_model 阶段:
  ...
  → ViewImageMiddleware
  → SubagentLimitMiddleware  ← 在这里
  → ClarificationMiddleware
  → ...
```

工作方式：计数最后一个 `AIMessage` 中名为 `"task"` 的 `tool_calls` 数量。超过限制则用截断后的工具调用列表替换该消息。这是**编程式强制**，LLM 无法绕过。

---

## 4. 错误处理、重试与超时

### 4.1 超时机制（多层防御）

| 层 | 代码位置 | 机制 |
|---|---|---|
| 线程池层 | `executor.py:684` | `execution_future.result(timeout=self.config.timeout_seconds)`，捕获 `FuturesTimeoutError` → 设置 cancel_event + 取消 future |
| 轮询安全网 | `task_tool.py:266` | `poll_count > max_poll_count`（`(timeout + 60) // 5`）→ 记录错误，字符串超时消息 |
| LangGraph 层 | `executor.py:413` | `recursion_limit = config.max_turns` → `GraphRecursionError` 被通用异常捕获 |
| 通用异常 | `executor.py:_aexecute` | `except Exception` → 状态 `FAILED`，`result.error = str(e)` |

### 4.2 错误传播链

```
_aexecute()  →  FAILED + error 字符串
     │
execute_async()  →  FuturesTimeoutError → TIMED_OUT
     │
execute()  →  通用 Exception → 新 FAILED 结果
     │
task_tool()  →  返回 "Error: ..." 字符串 → LLM 消费
```

### 4.3 协作取消

由于 Python 线程无法被强制杀死，子代理使用协作取消：

1. `cancel_event = threading.Event()` 存储在 `SubagentResult` 中
2. 每次流式迭代开始前检查：`if result.cancel_event.is_set(): break`
3. 父任务被取消或超时时，设置 `cancel_event`
4. 取消后启动一个新的异步协程，轮询后台线程进入终止状态

**局限**：长时间运行的工具调用（如等待外部 API 的 bash 命令）在迭代到达前无法被中断。

### 4.4 清理机制

- `cleanup_background_task(task_id)`：仅当任务处于**终止状态**（COMPLETED/FAILED/CANCELLED/TIMED_OUT）时，才从 `_background_tasks` 字典中删除
- 防止非终止状态任务的竞态条件
- 记录跳过清理操作以供调试

---

## 5. 生命周期管理

### 5.1 创建

| 阶段 | 触发点 | 产物 |
|---|---|---|
| 配置加载 | 启动时（config.yaml 解析） | 内置/自定义 SubagentConfig 对象 |
| 编译时 | `make_lead_agent()` / `create_deerflow_agent()` | 包含 `task` 工具的主代理图 |
| 运行时 | LLM 调用 `task` 工具 | `SubagentExecutor` 实例（每次工具调用新建） |

### 5.2 执行

- **后台模式**：子代理总是在后台执行，task_tool 中同步轮询
- **隔离性**：每个子代理执行有独立的 `SubagentExecutor`、独立的 LangGraph agent 和隔离的事件循环提交
- **状态共享**：子代理继承父级的 `sandbox_state` 和 `thread_data`，可访问相同沙箱环境

### 5.3 销毁与清理

| 项目 | 清理时机 | 机制 |
|---|---|---|
| `_background_tasks[task_id]` | 终止状态后 | `cleanup_background_task()` 删除字典条目 |
| 隔离事件循环 | 进程退出时 | `atexit` 注册 `_shutdown_isolated_subagent_loop()` |
| 事件循环线程 | 进程退出时 | 守护线程，主线程退出时自动终止 |
| `_scheduler_pool` | 进程退出时 | ThreadPoolExecutor 守护线程 |
| 子代理 LangGraph agent | 每次 `_aexecute()` 后 | GC 回收 |
| 后台线程异常 | `execute_async()` 回调 | `run_task()` 的 except 块处理 |

---

## 6. 配置解析层次（registry.py）

`get_subagent_config()` 的四层解析：

```
请求的名称
    │
    ├── 第 1 步：BUILTIN_SUBAGENTS 查找
    │      ├── 找到？使用内置配置
    │      └── 未找到？进入第 2 步
    │
    ├── 第 2 步：config.yaml custom_agents 查找
    │      ├── 找到？使用自定义配置
    │      └── 未找到？返回 None
    │
    ├── 第 3 步：应用 config.yaml 覆盖（agents 部分）
    │
    └── 第 4 步：最终化（补全默认值）
```

覆盖规则差异：

| 字段 | 内置代理 | 自定义代理 |
|---|---|---|
| timeout_seconds | 每代理覆盖 → 全局默认 → 内置默认 | **仅**每代理覆盖（不回退全局默认） |
| max_turns | 每代理覆盖 → 全局默认 → 内置默认 | **仅**每代理覆盖 |
| model | **仅**每代理覆盖（无全局默认） | **仅**每代理覆盖 |
| skills | **仅**每代理覆盖（无全局默认） | **仅**每代理覆盖 |

**关键设计决策**：自定义代理定义了自己的默认值，全局默认值不覆盖自定义代理的值。防止部署范围的配置意外改变用户定义的子代理行为。

---

## 7. 安全考量

| 安全措施 | 实现 |
|---|---|
| **工具过滤** | 每个子代理用 allowlist/denylist 限制可用工具 |
| **禁止递归嵌套** | `task` 工具从子代理工具列表排除（`subagent_enabled=False` + `disallowed_tools=["task"]`） |
| **SSH 沙箱限制** | `is_host_bash_allowed()` 检查；本地沙箱在宿主机运行时隐藏 bash 子代理 |
| **沙箱隔离** | 子代理继承父级沙箱状态，受相同路径映射和容器隔离限制 |
| **技能隔离** | 子代理加载自己的技能，由 `config.skills` 白名单控制 |
| **并发限制** | `SubagentLimitMiddleware` 限制单次 LLM 响应中的并发 task 调用 ≤ 3 |

---

## 8. 已知设计局限

1. **轮询开销**：task_tool 阻塞主代理异步流，每 5 秒轮询占用事件循环
2. **无增量流到父级**：子代理 LLM 流在内部消费，仅最终 AI 消息冒泡到父级
3. **线程模型排队**：高并发（>3 并行子代理）在 `_scheduler_pool` 中排队
4. **协作取消局限**：长时间工具调用在流迭代到达前无法中断
5. **技能加载 I/O 阻塞**：大技能文件使用 `asyncio.to_thread` 进行磁盘 I/O，会阻塞子代理事件循环线程
6. **潜在内存泄漏**：task_tool 轮询在完成前被取消后，若延迟清理轮询超时，`_background_tasks` 条目可能被孤立
7. **Executor 配置解析两次**：`SubagentExecutor.__init__` 预解析 model_name，但 `_create_agent()` 在 `self.model_name is None` 时可能再次解析
