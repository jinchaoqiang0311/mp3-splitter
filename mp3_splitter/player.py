"""基于 Windows MCI 的轻量播放器,支持从指定位置播放到指定位置(用于试听)。

仅在 Windows 上可用;其他平台或调用失败时,``available`` 为 False,
调用方应禁用试听相关按钮。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

__all__ = ["MciPlayer", "MciError"]

_ALIAS = "qoder_split_preview"


class MciError(RuntimeError):
    """MCI 调用失败。"""


def _short_path(path: str) -> str:
    """MCI 对长文件名的支持不佳,尽量转换为 8.3 短路径。"""
    if os.name != "nt":
        return path
    try:
        size = ctypes.windll.kernel32.GetShortPathNameW(path, None, 0)
        if size > 0:
            buf = ctypes.create_unicode_buffer(size)
            if ctypes.windll.kernel32.GetShortPathNameW(path, buf, size):
                return buf.value
    except Exception:
        pass
    return path


class MciPlayer:
    """简单的单文件播放器封装:open / play(x, y) / pause / resume / stop / close。"""

    def __init__(self) -> None:
        self.available = os.name == "nt"
        self._opened = False
        self._winmm = None
        if self.available:
            try:
                self._winmm = ctypes.windll.winmm
                self._winmm.mciSendStringW.argtypes = [
                    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HWND
                ]
                self._winmm.mciSendStringW.restype = wintypes.DWORD
                self._winmm.mciGetErrorStringW.argtypes = [
                    wintypes.DWORD, wintypes.LPWSTR, wintypes.UINT
                ]
            except Exception:
                self.available = False
                self._winmm = None

    # ------------------------------------------------------------- 内部工具
    def _send(self, command: str) -> str:
        buf = ctypes.create_unicode_buffer(260)
        code = self._winmm.mciSendStringW(command, buf, len(buf), None)
        if code != 0:
            err = ctypes.create_unicode_buffer(260)
            self._winmm.mciGetErrorStringW(code, err, len(err))
            raise MciError(err.value or f"MCI 错误码 {code}")
        return buf.value

    # ------------------------------------------------------------- 对外接口
    def open(self, path: str) -> None:
        """打开音频文件(会先关闭当前文件)。"""
        if not self.available:
            raise MciError("当前系统不支持内置播放")
        if not os.path.isfile(path):
            raise MciError(f"文件不存在:{path}")
        self.close()
        self._send(f'open "{_short_path(path)}" type mpegvideo alias {_ALIAS}')
        self._send(f"set {_ALIAS} time format milliseconds")
        self._opened = True

    def play(self, start_ms: int | None = None, end_ms: int | None = None) -> None:
        """从 start_ms 播放到 end_ms(毫秒)。end_ms 为 None 时播放到文件结尾。"""
        if not self._opened:
            return
        parts = [f"play {_ALIAS}"]
        if start_ms is not None:
            parts.append(f"from {max(0, int(start_ms))}")
        if end_ms is not None:
            parts.append(f"to {max(0, int(end_ms))}")
        self._send(" ".join(parts))

    def pause(self) -> None:
        if self._opened:
            self._send(f"pause {_ALIAS}")

    def resume(self) -> None:
        if self._opened:
            self._send(f"resume {_ALIAS}")

    def stop(self) -> None:
        if self._opened:
            self._send(f"stop {_ALIAS}")

    def seek(self, ms: int) -> None:
        if self._opened:
            self._send(f"seek {_ALIAS} to {max(0, int(ms))}")

    def position_ms(self) -> int:
        if not self._opened:
            return 0
        try:
            return int(self._send(f"status {_ALIAS} position") or 0)
        except (MciError, ValueError):
            return 0

    def mode(self) -> str:
        """返回 playing / paused / stopped / not ready 等状态。"""
        if not self._opened:
            return "closed"
        try:
            return self._send(f"status {_ALIAS} mode").strip().lower()
        except MciError:
            return "closed"

    def is_playing(self) -> bool:
        return self.mode() == "playing"

    def close(self) -> None:
        if self._opened and self._winmm is not None:
            try:
                self._send(f"close {_ALIAS}")
            except MciError:
                pass
        self._opened = False
