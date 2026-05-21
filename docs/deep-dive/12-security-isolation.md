# 用户隔离与安全 — 四层防御纵深

> DeerFlow Agent Harness 深度分析 · 第 12 篇

---

## 1. 概述与定位

安全系统是 DeerFlow 的护城河——四层防御纵深确保不同用户的数据隔离、操作授权、命令安全和循环防护。

### 一句话设计哲学

**"安全是纵深防御——一层被绕过，下一层仍然有效。"**

---

## 2. 架构总览

### 2.1 四层防御

```
┌──────────────────────────────────────────────────────────────┐
│ L1: ContextVar 身份                                          │
│   deerflow_current_user → 请求级用户 ID                       │
├──────────────────────────────────────────────────────────────┤
│ L2: Auth + 授权                                              │
│   JWT 认证 → @require_permission → Owner check               │
├──────────────────────────────────────────────────────────────┤
│ L3: 文件系统隔离                                              │
│   .deer-flow/users/{user_id}/threads/{thread_id}/            │
├──────────────────────────────────────────────────────────────┤
│ L4: 应用层安全                                                │
│   GuardrailMiddleware → SandboxAuditMiddleware               │
│   → LoopDetectionMiddleware → Host-bash 过滤                 │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. 源码走读

### 3.1 L1: ContextVar 身份传播

```python
deerflow_current_user: ContextVar[CurrentUser | None] = \
    ContextVar("deerflow_current_user", default=None)

def get_effective_user_id() -> str:
    """获取有效用户 ID，未设置时返回 "default"。"""
    user = deerflow_current_user.get()
    if user is not None:
        return user.id
    return "default"
```

**三态语义**：
- `AUTO`（默认）：从 ContextVar 读取，未设置则报错
- 显式 `str`：覆盖 ContextVar 值
- 显式 `None`：跳过 user_id WHERE 子句（迁移/CLI 用）

### 3.2 L2: JWT 认证 + 授权

#### AuthMiddleware

```python
class AuthMiddleware:
    async def __call__(self, request: Request, call_next):
        # 从 cookie 提取 JWT
        token = request.cookies.get("auth_token")
        if token:
            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
                user = CurrentUser(id=payload["sub"])
                deerflow_current_user.set(user)  # 设置 ContextVar
            except jwt.InvalidTokenError:
                pass
        response = await call_next(request)
        return response
```

#### @require_permission

```python
def require_permission(resource: str, action: str, owner_check: bool = False):
    def decorator(func):
        async def wrapper(request, *args, **kwargs):
            auth: AuthContext = request.state.auth
            if not auth.is_authenticated:
                raise HTTPException(401)

            # 权限检查
            if not auth.has_permission(f"{resource}:{action}"):
                raise HTTPException(403)

            # Owner check（所有权验证）
            if owner_check:
                thread_id = kwargs.get("thread_id")
                if not ThreadMetaStore.check_access(thread_id, auth.user_id):
                    raise HTTPException(404)  # 404 而非 403，不泄露存在性

            return await func(request, *args, **kwargs)
        return wrapper
    return decorator
```

**权限模型**：

| 权限 | 描述 |
|------|------|
| `threads:read` | 读取线程 |
| `threads:write` | 修改线程 |
| `threads:delete` | 删除线程 |
| `runs:create` | 创建运行 |
| `runs:read` | 读取运行 |
| `runs:cancel` | 取消运行 |

**Owner check 细节**：
- 不同 `user_id` 的线程 → 404（不泄露存在性）
- 无主线程（遗留）→ 读允许，写拒绝

#### CSRF Double-Submit Cookie

```python
# 登录时设置
response.set_cookie("auth_token", jwt_token, httponly=True, samesite="lax")
response.set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax")

# 请求时验证
if request.cookies.get("csrf_token") != request.headers.get("X-CSRF-Token"):
    raise HTTPException(403)
```

### 3.3 L3: 文件系统隔离

```
.deer-flow/
└── users/
    ├── alice/
    │   └── threads/
    │       ├── t001/    → Alice 的线程 1
    │       └── t002/    → Alice 的线程 2
    └── bob/
        └── threads/
            └── t003/    → Bob 的线程 3
```

`get_effective_user_id()` 被沙箱和路径模块调用，确保每个用户的文件操作限制在自己的目录下。

**路径遍历防护**：

```python
def _resolve_path(self, virtual_path: str) -> str:
    """解析虚拟路径，防止路径遍历。"""
    real_path = ...  # 解析逻辑
    # 验证解析后的路径在用户目录内
    if not Path(real_path).resolve().startswith(Path(user_dir).resolve()):
        raise ValueError("Path traversal detected")
    return real_path
```

### 3.4 L4: 应用层安全

#### GuardrailMiddleware — 可插拔护栏

```python
class GuardrailMiddleware(AgentMiddleware[AgentState]):
    def __init__(self, provider: GuardrailProvider, fail_closed: bool = True):
        self._provider = provider
        self._fail_closed = fail_closed

    def wrap_tool_call(self, request, handler):
        try:
            result = self._provider.evaluate(request.tool_call)
        except Exception:
            if self._fail_closed:
                return ToolMessage(content="Tool call denied: guardrail error", ...)
            return handler(request)

        if result.denied:
            return ToolMessage(content=f"Tool call denied: {result.reason}", ...)
        return handler(request)
```

**GuardrailProvider Protocol**：

```python
class GuardrailProvider(Protocol):
    def evaluate(self, tool_call: dict) -> GuardrailResult: ...
    async def aevaluate(self, tool_call: dict) -> GuardrailResult: ...
```

**Fail-closed**：Provider 异常时默认阻止调用（安全优先）。

#### SandboxAuditMiddleware — 命令审计

（详见第 1 篇和第 5 篇的完整分析）

#### LoopDetectionMiddleware — 循环检测

（详见第 1 篇的完整分析）

#### Host-bash 过滤

```python
def is_host_bash_allowed(config: AppConfig) -> bool:
    """检查 LocalSandboxProvider 模式下是否允许 host-bash。"""
    if config.sandbox.type != "local":
        return True  # Docker 沙箱，bash 安全
    return config.sandbox.allow_host_bash  # 默认 False
```

### 3.5 孤儿线程迁移

```python
async def _migrate_orphaned_threads(admin_user_id: str) -> None:
    """将无主线程分配给管理员。"""
    all_threads = await client.threads.search()
    for thread in all_threads:
        meta = thread.get("metadata", {})
        if "user_id" not in meta:
            await client.threads.update(
                thread_id=thread["thread_id"],
                metadata={"user_id": admin_user_id},
            )
```

**场景**：系统从无 Auth 升级到有 Auth 时，已有线程没有 `user_id`。迁移将它们分配给管理员，避免成为"孤儿"。

---

## 4. 核心机制详解

### 4.1 ContextVar 在请求级身份传播中的角色

```
HTTP 请求到达
  │
  ├── AuthMiddleware → JWT 解码 → deerflow_current_user.set(user)
  │
  ├── [中间件链执行] → get_effective_user_id() → 读取 ContextVar
  │     ├── 沙箱路径解析 → /users/{user_id}/...
  │     ├── 记忆存储 → /memory/user-{user_id}/...
  │     └── 权限检查 → ThreadMetaStore.check_access(thread_id, user_id)
  │
  └── 请求结束 → ContextVar 自动清理
```

**关键**：ContextVar 是协程安全的——同一个事件循环中的不同协程有独立的值。这确保并发请求间不互相污染。

### 4.2 404 vs 403 的选择

```python
# Owner check 失败 → 404 而非 403
if not ThreadMetaStore.check_access(thread_id, auth.user_id):
    raise HTTPException(404)
```

**原因**：403 泄露资源存在性（"它存在但你没权限"），404 不泄露（"它不存在"）。这是 OWASP 推荐的做法。

### 4.3 纵深防御的实例

以 `bash` 工具调用为例，经过四层防御：

```
LLM 决定调用 bash("rm -rf /")
  │
  ├── L1: ContextVar → 确定用户身份
  ├── L2: @require_permission("runs:create") → 验证用户有权创建运行
  ├── L3: 文件系统隔离 → 命令在 /users/{user_id}/ 下执行
  ├── L4a: GuardrailMiddleware → 评估工具调用（可能拒绝）
  ├── L4b: SandboxAuditMiddleware → 检测 "rm -rf /" → BLOCKED
  │
  └── 返回错误 ToolMessage，命令未执行
```

即使 GuardrailMiddleware 放行（L4a），SandboxAuditMiddleware 仍然阻止（L4b）。即使审计被绕过，文件系统隔离（L3）限制影响范围。即使文件系统被突破，身份验证（L2）确保可追溯。

---

## 5. 设计模式提取

| 模式 | 应用 |
|------|------|
| **纵深防御** | 四层安全，每层独立有效 |
| **Protocol（结构化子类型）** | GuardrailProvider 不需要显式继承 |
| **Fail-closed** | 异常时默认拒绝（安全优先） |
| **Double-Submit Cookie** | CSRF 防护 |
| **ContextVar** | 请求级身份传播，协程安全 |

---

## 6. 业界对比

| 特性 | DeerFlow | Django | Spring Security | Auth0 |
|------|---------|--------|----------------|-------|
| **认证** | JWT + Cookie | Session + Cookie | 多种 | OAuth 2.0 |
| **授权** | RBAC + Owner check | RBAC | RBAC/ABAC | RBAC |
| **CSRF** | Double-Submit Cookie | CSRF Token | CSRF Token | N/A |
| **多租户** | 文件路径隔离 | DB 行级 | DB 行级 | N/A |
| **应用层安全** | Guardrail + Audit + Loop | 无 | 无 | 无 |

**DeerFlow 的独特之处**：应用层安全（Guardrail + Audit + Loop Detection）是 Agent 系统特有的，Web 框架不需要。

---

## 7. 面试关联

### Q1: Agent 系统的安全模型如何设计？

**加分项**：

> "DeerFlow 用**四层纵深防御**：L1 ContextVar 身份传播（请求级用户 ID，协程安全）；L2 JWT + RBAC + Owner check（认证、授权、所有权验证，404 而非 403 不泄露存在性）；L3 文件系统隔离（per-user 目录，路径遍历防护）；L4 应用层安全（GuardrailMiddleware 可插拔护栏 + SandboxAuditMiddleware 命令审计 + LoopDetectionMiddleware 循环检测 + Host-bash 过滤）。关键设计是 **Fail-closed**——GuardrailMiddleware 在 Provider 异常时默认阻止调用，因为安全系统中'默认拒绝'比'默认允许'更安全。"

### Q2: 多租户数据隔离策略？

**加分项**：

> "DeerFlow 用**文件路径隔离**——每个用户的数据存储在 `.deer-flow/users/{user_id}/` 下，通过 `get_effective_user_id()` 从 ContextVar 获取用户 ID，所有文件操作限制在用户目录内。这比 DB 行级隔离更简单但隔离性较弱（依赖路径验证而非 DB 约束）。优点是无额外基础设施（不需要多数据库或行级安全策略），缺点是路径验证可能有漏洞。DeerFlow 通过路径遍历防护（`Path.resolve().startswith(user_dir)`）和 AGENT_NAME_PATTERN 验证（防止 `../../etc` 类攻击）加固。"

---

## 8. 扩展思考

| 局限 | 改进方向 |
|------|---------|
| 文件路径隔离弱于 DB 行级 | 迁移到 DB 存储 + 行级安全策略 |
| JWT 无刷新机制 | 添加 refresh_token 轮换 |
| GuardrailProvider 无审计日志 | 记录所有评估结果（允许/拒绝/异常） |
| 无速率限制 | 添加 per-user API 速率限制 |
| 无输入验证 | Agent 输入（用户消息）应验证长度/格式 |
| 孤儿迁移仅启动时 | 添加定期后台扫描 |
