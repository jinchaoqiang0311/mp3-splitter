"""时间码解析与格式化工具。"""

from __future__ import annotations

import re

__all__ = ["parse_time", "fmt_clock"]

# 匹配 "数字 + 可选单位",用于解析 "5分30秒"、"90s"、"1.5h" 等写法
_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(小时|钟头|时|分钟|分|秒钟|秒|h|m|s)?")

_HOUR_UNITS = {"h", "时", "小时", "钟头"}
_MINUTE_UNITS = {"m", "分", "分钟"}


def parse_time(value) -> float | None:
    """把用户输入解析为秒数,无法解析时返回 None。

    支持的写法:
        ``"300"``、``"300s"``、``"5m"``、``"1.5h"``
        ``"5:00"``、``"1:02:03"``、``"5:00.5"``
        ``"5分30秒"``、``"1小时2分"``
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None

    text = str(value).strip().lower()
    if not text:
        return None
    text = text.replace("，", "").replace(",", ".").replace(" ", "")

    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3:
            return None
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if any(n < 0 for n in nums):
            return None
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return total

    total = 0.0
    matched = False
    for num, unit in _TOKEN_RE.findall(text):
        if not num:
            continue
        matched = True
        seconds = float(num)
        if unit in _HOUR_UNITS:
            seconds *= 3600
        elif unit in _MINUTE_UNITS:
            seconds *= 60
        total += seconds
    return total if matched else None


def fmt_clock(seconds: float, ms: bool = False) -> str:
    """把秒数格式化为 ``MM:SS`` / ``H:MM:SS``,``ms=True`` 时附带毫秒。"""
    seconds = max(0.0, float(seconds or 0.0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    core = f"{minutes:02d}:{secs:06.3f}" if ms else f"{minutes:02d}:{int(secs):02d}"
    return f"{hours}:{core}" if hours else core
