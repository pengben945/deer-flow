# 流式传输与运行时 — SSE 桥接

> DeerFlow Agent Harness 深度分析 · 第 10 篇

---

## 1. 概述与定位

流式传输系统将 LangGraph Agent 的执行过程实时推送给客户端，实现"边思考边展示"的用户体验。核心是 StreamBridge——一个生产者-消费者桥，连接 Agent 执行（生产者）和 SSE 响应（消费者）。

### 一句话设计哲学

**"StreamBridge 是 Agent 与客户端之间的管道——生产者推送事件，消费者订阅流，心跳保活，断连可恢复。"**

---

## 2. 架构总览

### 2.1 端到端 SSE 流

```
HTTP POST /api/threads/{id}/runs/stream
  │
  ├── RunManager.create_or_reject() → 创建运行记录
  ├── asyncio.Task(run_agent())     → 启动 Agent 执行
  │     │
  │     ├── LangGraph agent.astream()
  │     ├── StreamBridge.publish()   → 推送 SSE 事件
  │     └── StreamBridge.publish_end() → 发送终止标记
  │
  └── StreamingResponse(sse_consumer())
        ├── StreamBridge.subscribe()  → 订阅事件流
        ├── format_sse()             → 格式化 SSE 帧
        ├── heartbeat                → 空闲心跳
        └── on_disconnect            → 取消/继续策略
```

---

## 3. 源码走读

### 3.1 StreamBridge ABC

```python
class StreamBridge(ABC):
    @abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None: ...

    @abstractmethod
    async def publish_end(self, run_id: str) -> None: ...

    @abstractmethod
    async def subscribe(self, run_id: str, last_event_id: str | None = None,
                        heartbeat_interval: float = 15.0) -> AsyncIterator[StreamEvent]: ...

    @abstractmethod
    async def cleanup(self, run_id: str, delay: float = 0) -> None: ...
```

### 3.2 MemoryStreamBridge 实现

```python
class MemoryStreamBridge(StreamBridge):
    def __init__(self):
        self._streams: dict[str, asyncio.Queue[StreamEvent | None]] = {}
        self._event_counters: dict[str, int] = {}
        self._buffers: dict[str, list[StreamEvent]] = {}  # 用于重连重放

    async def publish(self, run_id, event, data):
        stream_event = StreamEvent(
            event=event,
            data=data,
            id=self._next_event_id(run_id),
        )
        # 缓冲（用于 Last-Event-ID 重连）
        self._buffers.setdefault(run_id, []).append(stream_event)
        # 推送到队列
        if run_id in self._streams:
            await self._streams[run_id].put(stream_event)

    async def subscribe(self, run_id, last_event_id=None, heartbeat_interval=15.0):
        queue = asyncio.Queue()
        self._streams[run_id] = queue

        # Last-Event-ID 重连：重放缓冲事件
        if last_event_id and run_id in self._buffers:
            for event in self._buffers[run_id]:
                if event.id > last_event_id:
                    await queue.put(event)

        # 心跳保活
        heartbeat_task = asyncio.create_task(
            self._heartbeat(run_id, queue, heartbeat_interval)
        )

        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval * 2)
                if event is None:  # END_SENTINEL
                    break
                yield event
        finally:
            heartbeat_task.cancel()
```

### 3.3 SSE 消费者

```python
async def sse_consumer(run_id, stream_bridge, request):
    """将 StreamBridge 事件转换为 SSE 帧。"""
    last_event_id = request.headers.get("Last-Event-ID")
    async for event in stream_bridge.subscribe(run_id, last_event_id):
        yield format_sse(event.event, event.data, event.id)

def format_sse(event: str, data: Any, event_id: int | None = None) -> str:
    """格式化 SSE 帧。"""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data) if not isinstance(data, str) else data}")
    lines.append("")  # 空行分隔
    return "\n".join(lines) + "\n"
```

**SSE 帧格式**：

```
id: 42
event: messages/tuple
data: {"role": "assistant", "content": "Hello"}

```

### 3.4 断连策略

```python
on_disconnect = request.query_params.get("on_disconnect", "cancel")

if client_disconnected:
    if on_disconnect == "cancel":
        # 取消后台 Agent 执行
        background_task.cancel()
    elif on_disconnect == "continue":
        # Agent 继续执行，结果可通过 GET /runs/{id} 获取
        pass
```

---

## 4. 核心机制详解

### 4.1 Last-Event-ID 重连

```
客户端连接 → 接收事件 id: 1, 2, 3
网络中断 → 客户端重连，发送 Last-Event-ID: 3
服务端 → 从缓冲重放 id > 3 的事件 → 继续流式传输
```

### 4.2 心跳保活

```python
async def _heartbeat(self, run_id, queue, interval):
    """定期发送心跳注释，防止连接超时。"""
    while True:
        await asyncio.sleep(interval)
        await queue.put(StreamEvent(event="heartbeat", data=None))
```

SSE 心跳是 `": heartbeat\n\n"` 格式的注释行，客户端忽略但保持连接活跃。

---

## 5. 设计模式提取

| 模式 | 应用 |
|------|------|
| **生产者-消费者** | StreamBridge 连接 Agent 执行与 SSE 响应 |
| **观察者** | subscribe() 返回 AsyncIterator，消费者按需消费 |
| **命令** | 断连策略（cancel/continue）是命令模式 |
| **备忘录** | 事件缓冲支持 Last-Event-ID 重连重放 |

---

## 6. 业界对比

| 特性 | DeerFlow SSE | WebSocket | Long Polling |
|------|-------------|-----------|-------------|
| **方向** | 单向（服务端→客户端） | 双向 | 单向 |
| **重连** | Last-Event-ID 原生支持 | 需自行实现 | 需自行实现 |
| **代理兼容** | HTTP/1.1 友好 | 需升级 | 友好 |
| **复杂度** | 低 | 中 | 低 |
| **心跳** | SSE 注释 | ping/pong 帧 | 轮询间隔 |

**DeerFlow 选择 SSE 的理由**：LangGraph Platform 使用 SSE，DeerFlow 保持兼容；SSE 原生支持 Last-Event-ID 重连；HTTP/1.1 代理友好。

---

## 7. 面试关联

### Q1: SSE 流式传输的实现细节？

**加分项**：

> "DeerFlow 的 SSE 实现基于 StreamBridge 生产者-消费者桥。生产者（Agent 执行）通过 `publish()` 推送事件到 per-run 的 `asyncio.Queue`，消费者（HTTP 响应）通过 `subscribe()` 的 AsyncIterator 消费事件。关键细节：**事件缓冲**——每个事件存入 `_buffers[run_id]` 列表，支持 `Last-Event-ID` 重连时重放；**心跳保活**——定期发送 SSE 注释行（`: heartbeat`），防止代理/负载均衡器超时；**断连策略**——`on_disconnect=cancel` 取消后台执行，`on_disconnect=continue` 让 Agent 继续运行，结果可通过 GET API 获取。"

### Q2: 断连恢复策略？

**加分项**：

> "DeerFlow 支持两种断连恢复：一是 **Last-Event-ID 重连**——客户端重连时发送上次接收的事件 ID，服务端从缓冲重放后续事件，确保不丢失；二是 **continue 模式**——客户端断连后 Agent 继续执行，结果持久化到 checkpointer，客户端可通过 `GET /api/threads/{id}/runs/{id}` 获取完整结果。两种策略适用不同场景：重连适合短暂网络波动，continue 适合长时间运行的任务（如深度研究）。"

---

## 8. 扩展思考

| 局限 | 改进方向 |
|------|---------|
| 事件缓冲无上限 | 添加 max_buffer_size + LRU 逐出 |
| 无背压机制 | 添加消费者慢时暂停生产者 |
| 心跳间隔固定 | 根据网络延迟自适应调整 |
| 无多消费者支持 | 支持同一 run_id 的多个订阅者（如多标签页） |
