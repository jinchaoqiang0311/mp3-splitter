"""MP3 分割工具入口。

用法:
    python main.py
"""

from __future__ import annotations

import ctypes
import os
import sys


def _enable_dpi_awareness() -> None:
    """让窗口在高分屏上保持清晰。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> int:
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更高版本。")
        return 1
    _enable_dpi_awareness()
    from mp3_splitter.app import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
