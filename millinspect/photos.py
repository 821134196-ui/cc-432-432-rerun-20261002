"""本地目录疵点照片存储。

照片以 ``defect-<id>-<uuid>.bin`` 保存在数据目录下的 ``photos/`` 中，
库内只存相对路径。照片本身不做鉴权——鉴权由服务层的授权令牌完成。
"""
from __future__ import annotations

import os
import secrets
import struct
import zlib

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def save_photo(base_dir: str, defect_id: int, data: bytes, ext: str = "bin") -> str:
    """写入照片字节，返回相对照片目录的路径。"""
    photos_dir = os.path.join(base_dir, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    name = f"defect-{defect_id}-{secrets.token_hex(6)}.{ext}"
    path = os.path.join(photos_dir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return os.path.join("photos", name)


def abs_path(base_dir: str, rel_path: str) -> str:
    """相对路径 -> 绝对路径，并阻止目录穿越。"""
    photos_root = os.path.realpath(os.path.join(base_dir, "photos"))
    target = os.path.realpath(os.path.join(base_dir, rel_path))
    if os.path.commonpath([photos_root, target]) != photos_root:
        raise ValueError("非法照片路径")
    return target


def placeholder_png(label_rgb: tuple[int, int, int] = (145, 82, 54)) -> bytes:
    """生成一张极简纯色 PNG，供演示数据使用（无第三方依赖）。"""
    width = height = 64
    raw = b"".join(
        b"\x00" + bytes(label_rgb) * width for _ in range(height)
    )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_MAGIC
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
