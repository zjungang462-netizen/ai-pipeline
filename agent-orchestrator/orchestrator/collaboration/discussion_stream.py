"""逐 token 实时会诊事件批处理与 SSE 写入桥接。"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..contracts import Result, RunContext


class TokenBatcher:
    """将高频单字符/子词增量按字符阈值或时间间隔合并，降低 SQLite 写放大。"""

    def __init__(
        self,
        emit: Callable[[int, str], None],
        *,
        max_chars: int = 24,
        max_interval_s: float = 0.08,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._emit = emit
        self._max_chars = max(1, int(max_chars))
        self._max_interval_s = max(0.0, float(max_interval_s))
        self._clock = clock
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._size = 0
        self._seq = 0
        self._last_flush = clock()

    def push(self, delta: str) -> None:
        if not isinstance(delta, str) or not delta:
            return
        to_emit: tuple[int, str] | None = None
        with self._lock:
            self._buffer.append(delta)
            self._size += len(delta)
            now = self._clock()
            if self._size >= self._max_chars or (now - self._last_flush) >= self._max_interval_s:
                self._seq += 1
                to_emit = (self._seq, ''.join(self._buffer))
                self._buffer.clear()
                self._size = 0
                self._last_flush = now
        if to_emit is not None:
            self._emit(*to_emit)

    def flush(self) -> None:
        to_emit: tuple[int, str] | None = None
        with self._lock:
            if self._buffer:
                self._seq += 1
                to_emit = (self._seq, ''.join(self._buffer))
                self._buffer.clear()
                self._size = 0
                self._last_flush = self._clock()
        if to_emit is not None:
            self._emit(*to_emit)


class DiscussionEventSink:
    """将会诊角色的流式增量写入协作事件表，由现有 /api/runs/<id>/stream 实时推送。"""

    def __init__(
        self,
        store,
        run_id: str,
        topic_id: str,
        role: str,
        *,
        turn_id: str = '',
        max_chars: int = 24,
        max_interval_s: float = 0.08,
    ):
        self.store = store
        self.run_id = run_id
        self.topic_id = topic_id
        self.role = role
        self.turn_id = turn_id or ''
        self._key_scope = self.turn_id or self.role
        self._batcher = TokenBatcher(self._write_chunk, max_chars=max_chars, max_interval_s=max_interval_s)

    def _base_payload(self) -> dict:
        payload = {'thread_id': self.topic_id, 'role': self.role}
        if self.turn_id:
            payload['turn'] = self.turn_id
        return payload

    def start(self) -> None:
        self.store.event(
            f'disc:{self.topic_id}:{self._key_scope}:start',
            self.run_id,
            'discussion_turn_start',
            self._base_payload(),
        )

    def on_token(self, delta: str) -> None:
        self._batcher.push(delta)

    def _write_chunk(self, seq: int, delta: str) -> None:
        self.store.event(
            f'disc:{self.topic_id}:{self._key_scope}:tok:{seq}',
            self.run_id,
            'discussion_token',
            {**self._base_payload(), 'seq': seq, 'delta': delta},
        )

    def finish(self, *, status: str = 'done') -> None:
        self._batcher.flush()
        self.store.event(
            f'disc:{self.topic_id}:{self._key_scope}:end',
            self.run_id,
            'discussion_turn_end',
            {**self._base_payload(), 'status': status},
        )


def run_streaming_turn(
    adapter,
    prompt: str,
    workdir: Path,
    ctx: RunContext,
    sink: DiscussionEventSink,
) -> Result:
    """以逐 token 流式模式执行单角色只读会诊并保证事件完整收尾。"""
    sink.start()
    previous_stream = getattr(adapter, 'stream_tokens', False)
    previous_cb = getattr(adapter, 'on_token', None)
    status = 'failed'
    try:
        adapter.stream_tokens = True
        adapter.on_token = sink.on_token
        result = adapter.run(prompt, workdir, ctx)
        status = 'done' if result.ok else 'failed'
        return result
    finally:
        adapter.stream_tokens = previous_stream
        adapter.on_token = previous_cb
        sink.finish(status=status)
