"""Windows .cmd 垫片。cmd.exe 按控制台代码页读批处理文件，解释器路径含中文时不能按 UTF-8 写入。"""
from __future__ import annotations

import os
from pathlib import Path


def batch_safe(path) -> str:
    """路径含非 ASCII 字符时换成 8.3 短路径，换不了就原样返回。"""
    text = str(path)
    if text.isascii() or os.name != 'nt':
        return text
    import ctypes

    get_short = ctypes.windll.kernel32.GetShortPathNameW
    get_short.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    get_short.restype = ctypes.c_uint32
    length = get_short(text, None, 0)
    buffer = ctypes.create_unicode_buffer(length + 1)
    if length and get_short(text, buffer, length + 1) and buffer.value.isascii():
        return buffer.value
    return text


def _console_encoding() -> str:
    import ctypes

    code_page = ctypes.windll.kernel32.GetConsoleOutputCP() or ctypes.windll.kernel32.GetOEMCP()
    return 'utf-8' if code_page == 65001 else f'cp{code_page}'


def write_cmd_shim(shim: Path, *args: str) -> None:
    """写一个逐个参数加引号的 .cmd 垫片；以 % 开头的批处理字面量（如 %*）不加引号。"""
    body = '@echo off\r\n' + ' '.join(
        arg if arg.startswith('%') else f'"{batch_safe(arg)}"' for arg in args) + '\r\n'
    encoding = 'utf-8' if body.isascii() else _console_encoding()
    try:
        shim.write_text(body, encoding=encoding)
    except UnicodeEncodeError as error:
        raise RuntimeError(f'.cmd 垫片里有控制台代码页 {encoding} 写不出的路径: {shim}') from error
