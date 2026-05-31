"""In-memory stream bridge backed by an in-process event log."""

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║ 【Runtime - MemoryStreamBridge】 内存事件管道（Producer-Consumer 模式）       ║
# ║                                                                              ║
# ║ 核心数据结构: _RunStream (events list + asyncio.Condition + ended flag)       ║
# ║                                                                              ║
# ║ 生产者 (Worker): publish() → 追加事件到列表，notify_all() 通知消费者           ║
# ║ 消费者 (SSE):    subscribe() → await condition.wait() 阻塞等待新事件          ║
# ║                                                                              ║
# ║ 设计特性:                                                                     ║
# ║   - 事件缓冲区上限 256 条，超出时丢弃最早的事件                                 ║
# ║   - Last-Event-ID 支持断线重连                                               ║
# ║   - 15s 心跳机制 (asyncio.wait_for + TIMEOUT)                                ║
# ║   - 延迟清理 (cleanup delay=60s) 给迟到的 subscriber 机会                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent

logger = logging.getLogger(__name__)


@dataclass
class _RunStream:
    events: list[StreamEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    start_offset: int = 0


class MemoryStreamBridge(StreamBridge):
    """Per-run in-memory event log implementation.

    Events are retained for a bounded time window per run so late subscribers
    and reconnecting clients can replay buffered events from ``Last-Event-ID``.
    """

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        self._maxsize = queue_maxsize
        self._streams: dict[str, _RunStream] = {}
        self._counters: dict[str, int] = {}

    # -- helpers ---------------------------------------------------------------

    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
            self._counters[run_id] = 0
        return self._streams[run_id]

    def _next_id(self, run_id: str) -> str:
        self._counters[run_id] = self._counters.get(run_id, 0) + 1
        ts = int(time.time() * 1000)
        seq = self._counters[run_id] - 1
        return f"{ts}-{seq}"

    def _resolve_start_offset(self, stream: _RunStream, last_event_id: str | None) -> int:
        if last_event_id is None:
            return stream.start_offset

        for index, entry in enumerate(stream.events):
            if entry.id == last_event_id:
                return stream.start_offset + index + 1

        if stream.events:
            logger.warning(
                "last_event_id=%s not found in retained buffer; replaying from earliest retained event",
                last_event_id,
            )
        return stream.start_offset

    # -- StreamBridge API ------------------------------------------------------

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        # 生产者: 追加事件 + 通知消费者 (asyncio.Condition.notify_all)
        # 事件 ID 格式: {timestamp_ms}-{seq}，支持 Last-Event-ID 定位
        stream = self._get_or_create_stream(run_id)
        entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)
        async with stream.condition:
            stream.events.append(entry)
            if len(stream.events) > self._maxsize:
                overflow = len(stream.events) - self._maxsize
                del stream.events[:overflow]
                stream.start_offset += overflow
            stream.condition.notify_all()

    async def publish_end(self, run_id: str) -> None:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            stream.ended = True
            stream.condition.notify_all()

    # (学习注释) ★ subscribe — 消费者端 Async Iterator
    # 核心机制: asyncio.Condition.wait() 在没有新事件时阻塞等待
    # 三种唤醒场景:
    #   1. 生产者 notify_all() → 有新事件，正常 yield
    #   2. TimeoutError (15s 无新事件) → yield HEARTBEAT_SENTINEL
    #   3. stream.ended=True → yield END_SENTINEL 并 return
    # Last-Event-ID 支持: 从已读事件之后继续，不重复发送
    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            next_offset = self._resolve_start_offset(stream, last_event_id)

        while True:
            async with stream.condition:
                if next_offset < stream.start_offset:
                    logger.warning(
                        "subscriber for run %s fell behind retained buffer; resuming from offset %s",
                        run_id,
                        stream.start_offset,
                    )
                    next_offset = stream.start_offset

                local_index = next_offset - stream.start_offset
                if 0 <= local_index < len(stream.events):
                    entry = stream.events[local_index]
                    next_offset += 1
                elif stream.ended:
                    entry = END_SENTINEL
                else:
                    try:
                        await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                    except TimeoutError:
                        entry = HEARTBEAT_SENTINEL
                    else:
                        continue

            if entry is END_SENTINEL:
                yield END_SENTINEL
                return
            yield entry

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        self._streams.pop(run_id, None)
        self._counters.pop(run_id, None)

    async def close(self) -> None:
        self._streams.clear()
        self._counters.clear()
