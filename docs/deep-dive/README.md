# DeerFlow Agent Harness 深度源码分析

> 每篇文档覆盖三个维度：源码走读 + 架构设计 + 面试关联。中英混合，10000+ 字/篇。

## 全部完成 (12/12)

| # | 主题 | 文件 | 核心关键词 |
|---|------|------|-----------|
| 1 | 中间件链架构 | [01-middleware-chain.md](01-middleware-chain.md) | 14-18 中间件、严格顺序、RuntimeFeatures、洋葱模型、LoopDetection、Clarification |
| 2 | 工具系统 | [02-tool-system.md](02-tool-system.md) | 5 层组装、resolve_variable、DeferredToolRegistry、tool_search、ACP、去重 |
| 3 | 子智能体系统 | [03-subagent-system.md](03-subagent-system.md) | SubagentExecutor、编排者-工作者、三重限制、错误隔离、模型继承 |
| 4 | 记忆系统 | [04-memory-system.md](04-memory-system.md) | MemoryUpdateQueue、防抖、LLM 驱动提取、BeforeSummarizationHook、纠正/强化检测 |
| 5 | 沙箱系统 | [05-sandbox-system.md](05-sandbox-system.md) | Sandbox ABC、SandboxProvider、虚拟路径 /mnt/、命令审计两阶段、引用感知分割 |
| 6 | MCP 集成 | [06-mcp-integration.md](06-mcp-integration.md) | stdio/SSE/HTTP、OAuth 2.0、mtime 缓存、热重载、DeferredToolRegistry 协作 |
| 7 | 技能系统 | [07-skills-system.md](07-skills-system.md) | SKILL.md、安全门控、渐进加载、.skill ZIP 安装、编辑/回滚/历史 |
| 8 | 模型工厂 | [08-model-factory.md](08-model-factory.md) | create_chat_model、7 Provider 补丁、thinking/vision、credential_loader |
| 9 | 配置系统 | [09-config-system.md](09-config-system.md) | AppConfig Pydantic、单例+mtime 重载、ContextVar 栈、$ENV_VAR 解析 |
| 10 | 流式传输与运行时 | [10-streaming-runtime.md](10-streaming-runtime.md) | StreamBridge、SSE、Last-Event-ID 重连、心跳、RunManager |
| 11 | IM 渠道集成 | [11-im-channels.md](11-im-channels.md) | Channel ABC、MessageBus、7 平台、流式/非流式、ChannelStore |
| 12 | 用户隔离与安全 | [12-security-isolation.md](12-security-isolation.md) | 四层隔离、ContextVar、JWT、RBAC、GuardrailProvider、循环检测 |

## 文档结构模板

每篇文档遵循统一结构：

1. **概述与定位** — 在整体架构中的位置、解决的核心问题、设计哲学
2. **架构总览** — ASCII 架构图、核心抽象与接口、交互关系
3. **源码走读** — 逐文件分析、关键函数签名、数据流追踪
4. **核心机制详解** — 最关键 2-3 个机制的深入剖析
5. **设计模式提取** — 使用的模式、动机与适用场景
6. **业界对比** — 同类系统实现对比、DeerFlow 的取舍
7. **面试关联** — 高频面试问题、标准回答、加分项
8. **扩展思考** — 局限与改进、重新设计、前沿关联
