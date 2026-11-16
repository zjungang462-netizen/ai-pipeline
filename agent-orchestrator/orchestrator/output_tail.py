"""按字符保留有界输出尾部，追加时只复制小块，读取结果时一次拼接。"""
from collections import deque


class TextTail:
    # 解码后的大小以字符计，与原先字符串切片一致；不按 UTF-8 字节裁剪。
    _BLOCK_CHARS = 4096

    def __init__(self, limit):
        if type(limit) is not int or limit <= 0:
            raise ValueError('输出尾部上限必须为正整数')
        self.limit = limit
        self._chunks = deque()
        self._size = 0

    def append(self, value):
        if not value:
            return
        if len(value) >= self.limit:
            self._chunks.clear()
            self._size = 0
            value = value[-self.limit:]
        offset = 0
        if self._chunks and len(self._chunks[-1]) < self._BLOCK_CHARS:
            # 合并短读，避免连续单字符输入累积数百万个 deque 节点。
            count = min(self._BLOCK_CHARS-len(self._chunks[-1]), len(value))
            self._chunks[-1] += value[:count]
            self._size += count
            offset = count
        for start in range(offset, len(value), self._BLOCK_CHARS):
            chunk = value[start:start+self._BLOCK_CHARS]
            self._chunks.append(chunk)
            self._size += len(chunk)
        excess = self._size-self.limit
        while excess > 0:
            first = self._chunks.popleft()
            if len(first) > excess:
                self._chunks.appendleft(first[excess:])
                self._size -= excess
                break
            self._size -= len(first)
            excess -= len(first)

    def getvalue(self):
        return ''.join(self._chunks)
