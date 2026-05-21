# 模型工厂 — 多 Provider 统一接入

> DeerFlow Agent Harness 深度分析 · 第 8 篇

---

## 1. 概述与定位

模型工厂是 DeerFlow 的 LLM 接入层——将配置驱动的模型定义解析为 LangChain `BaseChatModel` 实例，处理 thinking mode、vision、provider 特殊逻辑和凭据加载。

### 一句话设计哲学

**"配置声明意图，工厂处理差异——同一个 `create_chat_model()` 调用，适配 7 种 Provider。"**

---

## 2. 架构总览

### 2.1 模型解析流程

```
create_chat_model(name, thinking_enabled)
  │
  ├── 1. AppConfig.get_model_config(name) → ModelConfig
  ├── 2. resolve_class(model_config.use, BaseChatModel) → 动态导入
  ├── 3. 序列化设置（排除元数据字段）
  ├── 4. Thinking mode 处理（4 分支）
  ├── 5. Provider 特殊处理（Codex/MindIE/stream_usage）
  ├── 6. 实例化 model_class(**settings)
  └── 7. 附加 tracing callbacks
```

### 2.2 Provider 矩阵

| Provider | 类 | Thinking 方式 | 特殊处理 |
|----------|---|-------------|---------|
| OpenAI | `ChatOpenAI` | `extra_body.thinking` | stream_usage 自动启用 |
| Anthropic | `ChatAnthropic` | 原生 `thinking` 参数 | OAuth token 注入 |
| DeepSeek | `ChatOpenAI` (patched) | `reasoning_content` 重写 | 流式响应补丁 |
| vLLM/Qwen | `ChatOpenAI` | `chat_template_kwargs` | thinking 标签控制 |
| MiniMax | `ChatOpenAI` (patched) | 非标准字段归一化 | 流式响应补丁 |
| Codex | `CodexChatModel` | `reasoning_effort` | 剥离 max_tokens |
| MindIE | `MindIEChatModel` | N/A | 保守重试默认值 |

---

## 3. 源码走读

### 3.1 `create_chat_model()` — 核心工厂

```python
def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    **kwargs
) -> BaseChatModel:
    config = app_config or get_app_config()
    model_name = name or config.models[0].name  # 默认取第一个
    model_config = config.get_model_config(model_name)
    if not model_config:
        raise ValueError(f"Model '{model_name}' not found in config")

    # 动态类加载
    model_class = resolve_class(model_config.use, BaseChatModel)

    # 序列化设置（排除元数据字段）
    exclude = {"use", "name", "display_name", "description",
               "supports_thinking", "supports_reasoning_effort",
               "when_thinking_enabled", "when_thinking_disabled",
               "thinking", "supports_vision"}
    model_settings = model_config.model_dump(exclude=exclude)

    # Thinking mode 处理
    if thinking_enabled and model_config.supports_thinking:
        effective_wte = model_config.when_thinking_enabled or {}
        if model_config.thinking:
            effective_wte = _deep_merge_dicts(effective_wte, {"thinking": model_config.thinking})
        model_settings.update(effective_wte)
    elif model_config.supports_thinking:
        # 禁用 thinking（4 分支）
        ...

    # Provider 特殊处理
    if issubclass(model_class, CodexChatModel):
        model_settings.pop("max_tokens", None)
        model_settings["reasoning_effort"] = "none" if not thinking_enabled else "medium"
    if model_class.__name__ == "MindIEChatModel":
        model_settings.setdefault("max_retries", 1)

    # stream_usage 自动启用
    _enable_stream_usage_by_default(model_class, model_settings)

    # 实例化
    model_instance = model_class(**kwargs, **model_settings)

    # 附加追踪
    callbacks = build_tracing_callbacks()
    if callbacks:
        model_instance.callbacks = (model_instance.callbacks or []) + callbacks

    return model_instance
```

### 3.2 Thinking Mode 禁用的 4 分支

```python
# 分支 A：显式禁用配置
if model_config.when_thinking_disabled:
    model_settings.update(model_config.when_thinking_disabled)

# 分支 B：OpenAI 兼容网关
elif effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
    model_settings.setdefault("extra_body", {})
    model_settings["extra_body"]["thinking"] = {"type": "disabled"}
    model_settings["reasoning_effort"] = "minimal"

# 分支 C：vLLM/Qwen
elif any(k in effective_wte.get("extra_body", {}).get("chat_template_kwargs", {})
         for k in ("thinking", "enable_thinking")):
    model_settings.setdefault("extra_body", {})
    model_settings["extra_body"]["chat_template_kwargs"] = \
        _vllm_disable_chat_template_kwargs(
            effective_wte["extra_body"]["chat_template_kwargs"]
        )

# 分支 D：原生 langchain_anthropic
elif effective_wte.get("thinking", {}).get("type"):
    model_settings["thinking"] = {"type": "disabled"}
```

**为什么需要 4 分支？** 不同 Provider 对 thinking mode 的实现完全不同：
- OpenAI 用 `extra_body.thinking.type`
- Anthropic 用原生 `thinking` 构造参数
- vLLM 用 `chat_template_kwargs` 控制提示词模板
- 显式配置优先级最高

### 3.3 凭据加载

#### Claude Code OAuth

```python
def load_claude_code_credential() -> ClaudeCodeCredential | None:
    """查找顺序："""
    # 1. 环境变量 $CLAUDE_CODE_OAUTH_TOKEN / $ANTHROPIC_AUTH_TOKEN
    # 2. 文件描述符 $CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR
    # 3. 自定义路径 $CLAUDE_CODE_CREDENTIALS_PATH
    # 4. 默认路径 ~/.claude/.credentials.json
```

**Token 过期检查**：`time.time() * 1000 > expires_at - 60_000`（1 分钟缓冲）

**OAuth Beta Header**：`"oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2025-05-14"`

#### Codex CLI

```python
def load_codex_cli_credential() -> CodexCliCredential | None:
    """从 $CODEX_AUTH_PATH 或 ~/.codex/auth.json 读取。"""
    # 支持两种 JSON 结构：
    # 旧版: {"access_token": "...", "account_id": "..."}
    # 新版: {"tokens": {"access_token": "...", "account_id": "..."}}
```

### 3.4 Provider 补丁

#### DeepSeek 流式补丁

```python
# Monkey-patch ChatOpenAI._stream
# DeepSeek 返回 reasoning_content 而非 content
# 补丁将 reasoning_content 重写为 content
```

#### OpenAI 兼容网关 Token 用量补丁

```python
# 某些网关（Doubao、vLLM）返回非标准 usage 格式
# 补丁归一化 usage 字段，确保 TokenUsageMiddleware 正确记录
```

---

## 4. 核心机制详解

### 4.1 `thinking` 快捷字段

```yaml
# config.yaml 中的快捷方式
models:
  - name: claude-sonnet
    use: langchain_anthropic:ChatAnthropic
    thinking:
      type: enabled
      budget_tokens: 10000
```

等价于：

```yaml
models:
  - name: claude-sonnet
    use: langchain_anthropic:ChatAnthropic
    when_thinking_enabled:
      thinking:
        type: enabled
        budget_tokens: 10000
```

`create_chat_model()` 将 `thinking` 字段合并到 `when_thinking_enabled.thinking` 中。

### 4.2 stream_usage 自动启用

```python
def _enable_stream_usage_by_default(model_class, model_settings):
    """对 OpenAI 兼容模型 + 自定义 base_url，自动启用 stream_usage。"""
    if model_class.__module__.startswith("langchain_openai"):
        if model_settings.get("base_url") or model_settings.get("openai_api_base"):
            model_settings.setdefault("stream_usage", True)
    # 任何声明了 stream_usage 字段的模型类
    if "stream_usage" in model_class.model_fields:
        model_settings.setdefault("stream_usage", True)
```

**动机**：`TokenUsageMiddleware` 需要 `stream_usage=True` 才能从流式响应中提取 token 用量。自定义 base_url 的模型（vLLM、Doubao）通常支持此功能但默认不启用。

---

## 5. 设计模式提取

| 模式 | 应用 |
|------|------|
| **工厂方法** | `create_chat_model()` 根据配置创建不同模型实例 |
| **策略** | Thinking mode 的 4 分支是策略选择 |
| **适配器** | Provider 补丁将非标准 API 适配为 LangChain 标准格式 |
| **模板方法** | 工厂定义骨架（解析→设置→thinking→实例化→追踪），Provider 特殊处理是钩子 |

---

## 6. 业界对比

| 特性 | DeerFlow | LiteLLM | OpenRouter | LangChain ChatModel |
|------|---------|---------|-----------|-------------------|
| **Provider 数量** | 7 (内置) | 100+ | 100+ | 10+ |
| **Thinking mode** | 4 分支适配 | 部分 | 无 | 无 |
| **OAuth** | Claude Code OAuth | 无 | 无 | 无 |
| **Provider 补丁** | 3 个流式补丁 | 统一接口 | 统一接口 | 无 |
| **动态类加载** | resolve_class | 统一入口 | 统一入口 | Python import |

**DeerFlow 的独特之处**：Thinking mode 的 4 分支适配和 Claude Code OAuth 集成是其他框架没有的。

---

## 7. 面试关联

### Q1: 多模型接入的抽象设计？

**加分项**：

> "DeerFlow 用**工厂方法 + 动态类加载**实现多模型统一接入。`create_chat_model(name)` 接受模型名，通过 `resolve_class(model_config.use, BaseChatModel)` 动态导入 LangChain 模型类，所有模型统一返回 `BaseChatModel` 接口。关键挑战是 **thinking mode 的 Provider 差异**——OpenAI 用 `extra_body.thinking`，Anthropic 用原生 `thinking` 参数，vLLM 用 `chat_template_kwargs`，Codex 用 `reasoning_effort`。DeerFlow 用 4 分支策略适配，配置中 `when_thinking_enabled`/`when_thinking_disabled` 声明每个 Provider 的具体参数，工厂根据 Provider 类型选择正确的分支。"

### Q2: thinking/reasoning mode 的实现差异？

**加分项**：

> "Thinking mode 有三种实现范式：**原生 API 支持**（Anthropic 的 `thinking` 参数、OpenAI 的 `reasoning_effort`）、**提示词模板控制**（vLLM 的 `chat_template_kwargs` 控制是否在提示词中包含 thinking 标签）、**非标准字段重写**（DeepSeek 在流式响应中返回 `reasoning_content` 字段而非标准 `content`）。DeerFlow 的工厂统一处理这三种范式，配置声明意图（`thinking_enabled: bool`），工厂处理差异（选择正确的参数映射）。"

---

## 8. 扩展思考

| 局限 | 改进方向 |
|------|---------|
| Provider 补丁用 monkey-patch | 贡献上游或用子类覆盖 |
| 无模型能力自动检测 | 根据 API 响应自动检测 thinking/vision 支持 |
| 无 A/B 测试支持 | 支持模型路由（按请求特征选择模型） |
| 无模型性能监控 | 记录每个模型的延迟、token 用量、错误率 |
