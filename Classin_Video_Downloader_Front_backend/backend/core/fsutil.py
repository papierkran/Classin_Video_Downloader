"""文件路径与数据库文本之间的可逆编解码。

Why 需要它：Linux 文件名是任意字节串，`os.walk` 遇到非 UTF-8 字节时
会用 `surrogateescape` 解码成"代理字符"（U+DC80..U+DCFF）。这类字符
无法编码回 UTF-8，直接塞进 SQLite（内部按 UTF-8 存储）会抛
`UnicodeEncodeError: surrogates not allowed`——一次扫描里有 1 个坏名
文件就能让整批入库失败。

方案：对可正常 UTF-8 编码的路径原样存储（向后兼容既有数据）；对含代理
的路径加一个控制字符前缀 `\\u0001`，内容用 UTF-8(surrogateescape)→
Latin-1 做无损字节映射（Latin-1 单字节码位必然可存可读）。读回时逆变换，
保证 `open()`/`os.path.isfile()` 仍能命中真实文件。
"""
from __future__ import annotations

import os

# 前缀必须是"正常绝对路径不会以它开头"的字符；绝对路径以 '/' 开头，
# 故 U+0001（Start of Heading 控制符）作为哨兵既安全又几乎不可能撞车。
_SENTINEL = "\u0001"


def db_path(path: str) -> str:
    """库内存储形态：正常路径原样返回，含代理的路径转成可入库的字节映射。"""
    if not path:
        return path
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return _SENTINEL + path.encode("utf-8", "surrogateescape").decode("latin-1")
    return path


def fs_path(stored: str) -> str:
    """还原成文件系统可用路径（与 db_path 互逆，供 open/isfile/stat 使用）。"""
    if stored and stored.startswith(_SENTINEL):
        return stored[len(_SENTINEL):].encode("latin-1").decode("utf-8", "surrogateescape")
    return stored


def display_name(name: str) -> str:
    """展示用文本：把无法编码的代理字节替换成 U+FFFD，避免污染 UI/日志。"""
    if not name:
        return name
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def fsencode_key(*parts: str) -> bytes:
    """把若干片段拼成哈希/键用的字节串（经 fsencode 吸收代理字节）。"""
    return b":".join(os.fsencode(p) for p in parts)
