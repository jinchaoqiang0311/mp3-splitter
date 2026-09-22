"""GUI 冒烟测试:真实启动窗口,加载样例、模拟交互、执行一次分割并截图。

运行:
    python tests/smoke_gui.py
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import tkinter.messagebox as mb
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import make_sample, make_speech_like  # noqa: E402
from mp3_splitter.app import (  # noqa: E402
    APP_TITLE,
    DEFAULT_SENSITIVITY,
    SMART_MODE_LABELS,
    Mp3SplitterApp,
    create_root,
)
from mp3_splitter.audio_engine import (  # noqa: E402
    demucs_available,
    ffmpeg_path,
    make_preview_proxy,
    probe,
)

SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots")
SAMPLE_SECONDS = 180.0


def pump(root: tk.Tk, seconds: float) -> None:
    """保持 tk 事件循环转动给定秒数。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update()
        time.sleep(0.03)


def wait_until(root: tk.Tk, predicate, timeout: float, label: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError(f"等待超时:{label}")


class _BitmapInfoHeader(ctypes.Structure):
    """Win32 BITMAPINFOHEADER(供 PrintWindow 抓图使用)。"""

    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _grab_window_win32(hwnd: int):
    """用 PrintWindow 抓取窗口位图(锁屏或被遮挡时同样有效);失败返回 None。"""
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    window_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = user32.PrintWindow(hwnd, mem_dc, 2)  # PW_RENDERFULLCONTENT
        header = _BitmapInfoHeader()
        header.biSize = ctypes.sizeof(_BitmapInfoHeader)
        header.biWidth, header.biHeight = width, -height
        header.biPlanes, header.biBitCount, header.biCompression = 1, 32, 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)
    if not ok:
        return None
    return Image.frombuffer(
        "RGBA", (width, height), buffer, "raw", "BGRA", 0, 1
    ).convert("RGB")


def _image_has_content(image) -> bool:
    """截图是否包含有效画面(排除锁屏等造成的全黑图)。"""
    return image.convert("L").getextrema()[1] > 8


def grab(root: tk.Tk, name: str) -> str:
    os.makedirs(SHOT_DIR, exist_ok=True)
    root.update_idletasks()
    root.lift()
    root.update()
    time.sleep(0.25)  # 等界面把这一帧画完
    path = os.path.abspath(os.path.join(SHOT_DIR, name))
    try:
        image = _grab_window_win32(root.winfo_id())
    except Exception:  # noqa: BLE001 - 抓图失败时回退到屏幕抓取
        image = None
    if image is None or not _image_has_content(image):
        from PIL import ImageGrab

        root.attributes("-topmost", True)
        root.update()
        time.sleep(0.25)  # 等窗口真正上屏,避免被其他窗口遮挡
        x, y = root.winfo_rootx(), root.winfo_rooty()
        box = (x, y, x + root.winfo_width(), y + root.winfo_height())
        image = ImageGrab.grab(bbox=box, all_screens=True)
        root.attributes("-topmost", False)
    image.save(path)
    return path


class _DropEvent:
    """模拟 tkdnd 的拖放事件。"""

    def __init__(self, data: str) -> None:
        self.data = data


def tcl_list_data(root: tk.Tk, *paths: str) -> str:
    """生成 tkdnd 实际投递的拖放数据格式:规范 Tcl 列表(特殊字符自动加花括号)。"""
    for index, path in enumerate(paths):
        root.tk.setvar(f"_dnd_path_{index}", path)
    return root.tk.eval("list " + " ".join(f"$_dnd_path_{i}" for i in range(len(paths))))


def main() -> int:
    # 冒烟测试中屏蔽弹窗,避免阻塞
    mb.askyesno = lambda *a, **k: False
    mb.showinfo = lambda *a, **k: None
    mb.showerror = lambda *a, **k: None
    mb.showwarning = lambda *a, **k: None

    checks: list[tuple[str, bool]] = []

    root = create_root()
    root.title(APP_TITLE)
    root.geometry("1080x880+80+40")
    app = Mp3SplitterApp(root)
    pump(root, 0.5)

    tmp = tempfile.TemporaryDirectory()
    sample = os.path.join(tmp.name, "demo_song.mp3")
    print("生成测试音频(3 分钟)…")
    make_sample(sample, SAMPLE_SECONDS)

    print("通过界面加载文件…")
    app._load_file(sample)
    wait_until(root, lambda: app.info is not None, 120, "音频加载完成")
    checks.append(("音频信息解析", app.info is not None and abs(app.info.duration - 180) < 1.0))
    checks.append(("波形数据提取", len(app.peaks) > 500))
    checks.append(("播放器可用性", app._player_ready))
    print(f"  时长={app.info.duration:.2f}s 波形点={len(app.peaks)} 播放器={'可用' if app._player_ready else '不可用'}")

    # 尝试真实播放 0.6 秒验证 MCI 正常工作
    if app._player_ready:
        try:
            app._toggle_play()
            pump(root, 0.6)
            pos = app._player.position_ms()
            app._stop_play()
            pump(root, 0.2)
            checks.append(("试听播放推进", pos > 0))
            print(f"  播放位置推进到 {pos} ms")
        except Exception as exc:  # noqa: BLE001
            checks.append(("试听播放推进", False))
            print(f"  播放异常:{exc}")

    # 场景一:默认设置(自动每 5 分钟)
    pump(root, 0.4)
    shot1 = grab(root, "01_default_auto.png")
    print(f"  截图:{shot1}")

    # 场景二:每段 1 分钟 + 两个手动分割点(先关闭末段并入,应为 5 段)
    app._apply_preset(60)
    pump(root, 0.2)
    checks.append(("预设使用秒单位", app.chunk_var.get() == "60" and app._current_chunk() == 60.0))
    app.chunk_var.set("90.5")
    pump(root, 0.2)
    checks.append(("秒单位支持一位小数", app._current_chunk() == 90.5))
    app.chunk_var.set("60")
    pump(root, 0.2)
    app._add_cut(45.5)
    app._add_cut(132.25)
    app.merge_tail.set(False)
    app._refresh_preview()
    app._redraw_wave()
    app._set_playhead(100.0, follow=False)
    pump(root, 0.6)
    checks.append(("分段规划(1分钟+2个手动点)", len(app.segments) == 5))
    print(f"  分段数={len(app.segments)} 明细={[f'{s.start:.1f}-{s.end:.1f}' for s in app.segments]}")
    shot2 = grab(root, "02_manual_cuts.png")
    print(f"  截图:{shot2}")

    # 打开末段并入后,末段(47.75s < 60s)应并入前一段,变为 4 段
    app.merge_tail.set(True)
    app._refresh_preview()
    app._redraw_wave()
    pump(root, 0.6)
    checks.append(("末段并入前一段", len(app.segments) == 4))
    print(f"  分段数={len(app.segments)} 明细={[f'{s.start:.1f}-{s.end:.1f}' for s in app.segments]}")
    shot2b = grab(root, "02b_merged_tail.png")
    print(f"  截图:{shot2b}")

    # 场景三:执行真实分割
    out_dir = os.path.join(tmp.name, "output")
    app.outdir_var.set(out_dir)
    app.prefix_var.set("demo_part_")
    print("执行分割…")
    app._start_split()
    wait_until(root, lambda: not app._busy, 180, "分割完成")
    pump(root, 0.5)
    produced = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    checks.append(("输出文件数量", len(produced) == len(app.segments)))
    print(f"  输出:{produced}")

    ok_durations = True
    for name, seg in zip(produced, app.segments):
        duration = probe(os.path.join(out_dir, name)).duration
        if abs(duration - seg.duration) > 0.35:
            ok_durations = False
        print(f"  {name}: {duration:.2f}s (期望 {seg.duration:.2f}s)")
    checks.append(("输出时长匹配", ok_durations))

    shot3 = grab(root, "03_split_done.png")
    print(f"  截图:{shot3}")

    # 场景三b:分段冗余(每段首尾各延长,相邻段重叠)
    app.redundancy_var.set("1.1")
    app._refresh_preview()
    app._redraw_wave()
    pump(root, 0.4)
    first, last = app.segments[0], app.segments[-1]
    checks.append(
        (
            "冗余首尾各延长",
            app._redundancy() == 1.1
            and abs(first.start) < 1e-6
            and abs(first.end - 46.6) < 0.01
            and abs(last.end - app.info.duration) < 0.01,
        )
    )
    checks.append(("冗余色带绘制", len(app.canvas.find_withtag("redundancy")) >= 1))
    print(f"  冗余 1.1s:首段 {first.start:.2f}-{first.end:.2f},末段 {last.start:.2f}-{last.end:.2f}")
    shot5 = grab(root, "05_redundancy.png")
    print(f"  截图:{shot5}")
    app.redundancy_var.set("0.0")
    app._refresh_preview()
    app._redraw_wave()
    pump(root, 0.2)

    # 场景四:撤销/删除分割点交互
    app.cut_list.selection_clear(0, "end")
    app.cut_list.selection_set(0)
    app._remove_selected_cut()
    pump(root, 0.3)
    checks.append(("删除分割点", len(app.manual_cuts) == 1))
    app._clear_cuts()
    pump(root, 0.3)
    checks.append(("清空分割点", len(app.manual_cuts) == 0))

    class _Evt:
        x: int = 0

    # 场景五:拖动分割点微调(90s 处拖到 75% 位置 ≈ 135s)
    app._add_cut(90.0)
    app._redraw_wave()
    pump(root, 0.3)
    canvas_width = app.canvas.winfo_width()
    press = _Evt()
    press.x = int(app._time_to_x(90.0))
    app._on_wave_press(press)
    drag = _Evt()
    drag.x = int(canvas_width * 0.75)
    app._on_wave_drag(drag)
    app._on_wave_release(drag)
    pump(root, 0.3)
    moved = app.manual_cuts[0] if app.manual_cuts else -1.0
    checks.append(("拖动分割点", abs(moved - 135.0) < 2.0))
    print(f"  拖动后分割点={moved:.2f}s(期望 ≈135s)")
    shot4 = grab(root, "04_drag_marker.png")
    print(f"  截图:{shot4}")

    # 场景六:右键删除分割点
    right = _Evt()
    right.x = int(app._time_to_x(moved))
    app._on_wave_right(right)
    pump(root, 0.2)
    checks.append(("右键删除分割点", len(app.manual_cuts) == 0))

    # 场景六b:双击列表行就地编辑分割点时间
    app._add_cut(100.0)
    app._redraw_wave()
    pump(root, 0.3)
    app.cut_list.selection_clear(0, "end")
    app.cut_list.selection_set(0)
    app._begin_edit_cut()
    pump(root, 0.3)
    started = app._edit_entry is not None
    checks.append(("双击进入就地编辑", started))
    shot6 = grab(root, "06_edit_cut.png")
    print(f"  截图:{shot6}")
    if started:
        entry = app._edit_entry
        entry.delete(0, "end")
        entry.insert(0, "1:40.5")  # 100.5 秒
        app._commit_edit_cut()
        pump(root, 0.3)
        edited = app.manual_cuts[0] if app.manual_cuts else -1.0
        checks.append(("编辑后分割点更新", abs(edited - 100.5) < 0.01))
        print(f"  编辑后分割点={edited:.3f}s(期望 100.5s)")

    # 场景六c:波形缩放与滚动
    app._zoom_reset()
    pump(root, 0.2)
    checks.append(("全览复位", abs(app.view_span - app.info.duration) < 1e-6))
    app._zoom(0.5, app.canvas.winfo_width() / 2)
    pump(root, 0.3)
    checks.append(("按钮缩放放大一倍", abs(app.view_span - app.info.duration / 2) < 0.5))
    print(f"  放大后可视窗口={app.view_span:.1f}s 起点={app.view_start:.1f}s")

    class _WheelEvt:
        x = 400
        delta = -120  # 滚轮向下 → 缩小

    span_before = app.view_span
    app._on_wheel(_WheelEvt())
    pump(root, 0.3)
    checks.append(("滚轮缩放", app.view_span > span_before))

    start_before = app.view_start
    app._on_wheel_shift(_WheelEvt())
    pump(root, 0.3)
    checks.append(("Shift+滚轮平移", app.view_start > start_before))
    print(f"  缩放后可视窗口={app.view_span:.1f}s 起点={app.view_start:.1f}s")

    app._set_view(100.0, 30.0)
    pump(root, 0.4)
    checks.append(
        ("视窗定位与滚动条同步", abs(app.view_start - 100.0) < 0.01 and abs(app.view_span - 30.0) < 0.01)
    )
    shot7 = grab(root, "07_zoom_detail.png")
    print(f"  截图:{shot7}")
    app._zoom_reset()
    pump(root, 0.3)

    # 场景七:非 MP3 输入自动禁用无损选项
    wav = os.path.join(tmp.name, "tone.wav")
    subprocess.run(
        [ffmpeg_path(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-ac", "2", "-ar", "44100", wav],
        check=True,
        capture_output=True,
    )
    app._load_file(wav)
    wait_until(
        root,
        lambda: app.info is not None and app.info.path.lower().endswith(".wav"),
        60,
        "wav 加载完成",
    )
    pump(root, 0.3)
    checks.append(
        ("非 MP3 自动禁用无损", "disabled" in app.lossless_check.state() and not app.lossless_var.get())
    )
    print(f"  非 MP3 输入:无损选项状态={app.lossless_check.state()}")

    # 场景八:人声/伴奏分离(轻量引擎) + 音源切换
    checks.append(
        (
            "引擎默认选择",
            app.engine_var.get() == ("ai" if app.demucs_ready else "light"),
        )
    )
    sep_dir = os.path.join(tmp.name, "separated")
    print("执行人声/伴奏分离(轻量引擎)…")
    app._start_separation(sep_dir, "light")
    wait_until(root, lambda: not app._busy, 120, "分离完成")
    pump(root, 0.5)
    checks.append(
        (
            "分离生成两个文件",
            bool(
                app.vocal_path
                and app.inst_path
                and os.path.isfile(app.vocal_path)
                and os.path.isfile(app.inst_path)
            ),
        )
    )
    if app.vocal_path and app.inst_path:
        v_dur = probe(app.vocal_path).duration
        i_dur = probe(app.inst_path).duration
        checks.append(("分离文件时长接近原曲", abs(v_dur - 6.0) < 0.5 and abs(i_dur - 6.0) < 0.5))
        print(
            f"  人声={os.path.basename(app.vocal_path)}({v_dur:.2f}s)  "
            f"伴奏={os.path.basename(app.inst_path)}({i_dur:.2f}s)"
        )
        shot8 = grab(root, "08_separated.png")
        print(f"  截图:{shot8}")

        # 载入人声为当前音源,并规划单独分割
        app._load_source("vocals")
        wait_until(
            root,
            lambda: app.info is not None and app.info.path == app.vocal_path,
            60,
            "人声载入",
        )
        pump(root, 0.4)
        checks.append(("载入人声音源", app.info is not None and app.info.path == app.vocal_path))
        app._apply_preset(3)
        app.merge_tail.set(False)
        app._refresh_preview()
        pump(root, 0.3)
        checks.append(("人声可单独分割", len(app.segments) >= 2))
        print(f"  人声分段数={len(app.segments)} 明细={[f'{s.start:.1f}-{s.end:.1f}' for s in app.segments]}")
        shot9 = grab(root, "09_vocals_loaded.png")
        print(f"  截图:{shot9}")

        # 切回原曲
        app._load_source("origin")
        wait_until(
            root,
            lambda: app.info is not None and app.info.path.lower().endswith(".wav"),
            60,
            "原曲载入",
        )
        pump(root, 0.4)
        checks.append(("切回原曲", app.info is not None and app.info.path.lower().endswith(".wav")))

    # 场景九:AI 引擎分离(需已安装 demucs,CPU 推理较慢)
    if demucs_available():
        sep_ai = os.path.join(tmp.name, "separated_ai")
        print("执行人声/伴奏分离(AI 引擎,首次运行较慢,请稍候)…")
        app.engine_var.set("ai")
        app._start_separation(sep_ai, "ai")
        wait_until(root, lambda: not app._busy, 900, "AI 分离完成")
        pump(root, 0.5)
        ai_ok = bool(
            app.vocal_path
            and app.inst_path
            and os.path.isfile(app.vocal_path)
            and os.path.isfile(app.inst_path)
        )
        checks.append(("AI 分离生成两个文件", ai_ok))
        if ai_ok:
            v_dur = probe(app.vocal_path).duration
            i_dur = probe(app.inst_path).duration
            checks.append(
                ("AI 分离文件时长接近原曲", abs(v_dur - 6.0) < 0.6 and abs(i_dur - 6.0) < 0.6)
            )
            print(
                f"  AI 人声={os.path.basename(app.vocal_path)}({v_dur:.2f}s)  "
                f"伴奏={os.path.basename(app.inst_path)}({i_dur:.2f}s)"
            )
            shot10 = grab(root, "10_ai_separated.png")
            print(f"  截图:{shot10}")
            app._load_source("vocals")
            wait_until(
                root,
                lambda: app.info is not None and app.info.path == app.vocal_path,
                60,
                "AI 人声载入",
            )
            pump(root, 0.4)
            checks.append(
                ("AI 人声可载入继续分割", app.info is not None and app.info.path == app.vocal_path)
            )
    else:
        print("  未安装 demucs,跳过 AI 引擎场景。")

    # 场景十:拖放载入文件(含空格路径 / 非音频拒绝)
    checks.append(("拖放功能已注册", app.dnd_ready))
    if app.dnd_ready:
        spaced = os.path.join(tmp.name, "拖入 的 音频.mp3")
        shutil.copyfile(sample, spaced)
        app._on_drag_enter(_DropEvent(""))
        pump(root, 0.2)
        checks.append(("拖拽悬停提示", "松开鼠标" in app.status_var.get()))

        app._on_drop(_DropEvent(tcl_list_data(root, spaced)))
        wait_until(
            root,
            lambda: app.info is not None and app.info.path == spaced,
            60,
            "拖放载入完成",
        )
        pump(root, 0.3)
        checks.append(("拖放载入含空格路径", app.info is not None and app.info.path == spaced))
        print(f"  拖放载入:{app.info.path}")
        checks.append(("拖放后提示恢复", "松开鼠标" not in app.status_var.get()))

        note = os.path.join(tmp.name, "note.txt")
        with open(note, "w", encoding="utf-8") as fh:
            fh.write("not audio")
        app._on_drop(_DropEvent(tcl_list_data(root, note)))
        pump(root, 0.2)
        checks.append(("拖放拒绝非音频", app.info is not None and app.info.path == spaced))
        print("  非音频文件已被拒绝")
        shot11 = grab(root, "11_drop_loaded.png")
        print(f"  截图:{shot11}")

    # 场景十一:视频容器提取音轨 + 试听自动转码
    proxy = make_preview_proxy(wav)
    checks.append(
        (
            "试听代理生成(引擎)",
            os.path.isfile(proxy) and abs(probe(proxy).duration - 6.0) < 0.5,
        )
    )
    print(f"  试听代理:{proxy}")
    shutil.rmtree(os.path.dirname(proxy), ignore_errors=True)

    video = os.path.join(tmp.name, "clip.mp4")
    subprocess.run(
        [
            ffmpeg_path(), "-y",
            "-f", "lavfi", "-i", "color=c=steelblue:s=320x240:d=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-shortest", video,
        ],
        check=True,
        capture_output=True,
    )
    # 兼容个别后端给出的未转义裸路径(反斜杠会被 Tcl 解析吞掉)
    checks.append(
        (
            "拖放数据兼容两种格式",
            app._parse_drop_paths(tcl_list_data(root, video)) == [video]
            and app._parse_drop_paths(video) == [video],
        )
    )
    app._on_drop(_DropEvent(tcl_list_data(root, video)))
    wait_until(
        root,
        lambda: app.info is not None and app.info.path == video,
        90,
        "视频容器载入完成",
    )
    pump(root, 0.4)
    checks.append(
        ("拖放载入视频容器", app.info is not None and abs(app.info.duration - 6.0) < 0.5)
    )
    checks.append(("视频音轨波形提取", len(app.peaks) > 50))
    print(
        f"  视频容器:{os.path.basename(video)} 时长={app.info.duration:.2f}s "
        f"波形点={len(app.peaks)}"
    )

    wait_until(root, lambda: app._player_ready, 90, "视频试听就绪(直接或转码副本)")
    checks.append(("视频试听可用", app._player_ready))
    checks.append(
        (
            "试听副本格式正确",
            app._preview_proxy is None
            or (
                app._preview_proxy.lower().endswith(".mp3")
                and os.path.isfile(app._preview_proxy)
            ),
        )
    )
    if app._preview_proxy:
        print(f"  试听副本:{os.path.basename(app._preview_proxy)}")

    shot12 = grab(root, "12_video_loaded.png")
    print(f"  截图:{shot12}")

    # 视频文件分割,输出 MP3(切成 2 段,避免触发"整段输出"确认框)
    out_v = os.path.join(tmp.name, "video_split")
    app.outdir_var.set(out_v)
    app.prefix_var.set("clip_")
    app.auto_enabled.set(True)
    app.chunk_var.set("3")
    app.merge_tail.set(True)
    app._refresh_preview()
    app._start_split()
    wait_until(root, lambda: not app._busy, 120, "视频分割完成")
    pump(root, 0.3)
    v_files = sorted(os.listdir(out_v)) if os.path.isdir(out_v) else []
    v_durs = [probe(os.path.join(out_v, name)).duration for name in v_files]
    checks.append(
        (
            "视频分割输出 MP3",
            len(v_files) == 2
            and all(name.endswith(".mp3") for name in v_files)
            and sum(v_durs) > 5.0,
        )
    )
    for name, dur in zip(v_files, v_durs):
        print(f"  视频输出:{name}({dur:.2f}s)")

    # 场景十二:智能分句(静音/呼吸检测 + 吸附切分)
    speech = os.path.join(tmp.name, "speech.mp3")
    print("生成说话样本(静音/呼吸检测)…")
    make_speech_like(speech)
    app._load_file(speech)
    wait_until(
        root,
        lambda: app.info is not None and app.info.path == speech,
        60,
        "说话样本载入",
    )
    wait_until(root, lambda: app._silences_ready(), 60, "静音/呼吸点检测完成")
    pump(root, 0.4)
    checks.append(("静音/呼吸点检测", len(app.silences) == 2))
    checks.append(("静音区波形标注", len(app.canvas.find_withtag("silence")) >= 2))
    checks.append(("灵敏度默认宽松", app.sensitivity_label.get() == DEFAULT_SENSITIVITY))
    print(f"  检测到静音区:{app.silences}")

    # 灵敏度切换:旧结果作废并按新档位重新检测
    app.sensitivity_label.set("严格")
    app._on_sensitivity_change()
    checks.append(("灵敏度切换作废旧结果", app._silences_path is None))
    wait_until(root, lambda: app._silences_ready(), 60, "严格档重新检测完成")
    pump(root, 0.3)
    checks.append(("严格档重新检测完成", len(app.silences) == 2))
    app.sensitivity_label.set(DEFAULT_SENSITIVITY)
    app._on_sensitivity_change()
    wait_until(root, lambda: app._silences_ready(), 60, "宽松档重新检测完成")
    pump(root, 0.3)
    print(f"  灵敏度切换后静音区:{app.silences}")

    # 吸附模式:每段 2 秒 → 切点 2/4/6 全部吸附到静音中点
    app.smart_enabled.set(True)
    app.smart_mode_label.set(SMART_MODE_LABELS["snap"])
    app.auto_enabled.set(True)
    app.chunk_var.set("2")
    app.merge_tail.set(False)  # 末段不足 2s 会被并入,先关闭以便验证两个切点都吸附
    app.snap_range_var.set("5.0")
    app._on_smart_change()
    pump(root, 0.4)
    snapped = [round(seg.end, 3) for seg in app.base_segments[:-1]]
    checks.append(
        (
            "自动切点吸附静音处",
            len(app.base_segments) == 3
            and len(snapped) == 2
            and all(any(s <= cut <= e for s, e in app.silences) for cut in snapped),
        )
    )
    print(f"  吸附后切点={snapped} 静音区={app.silences}")

    # 纯静音切分:忽略每段时长,只在静音/呼吸中点切分
    app.chunk_var.set("10")
    app.smart_mode_label.set(SMART_MODE_LABELS["silence"])
    app._on_smart_change()
    pump(root, 0.4)
    mids = [round((a + b) / 2, 3) for a, b in app.silences]
    boundaries = [round(seg.end, 3) for seg in app.base_segments[:-1]]
    checks.append(
        (
            "纯静音切分模式",
            len(app.base_segments) == 3
            and boundaries == mids
            and "disabled" in app.chunk_spin.state(),
        )
    )
    print(f"  纯静音切分:切点={boundaries}(静音中点={mids})")

    # 一键把静音点加入手动分割点列表
    app._add_silence_cuts()
    pump(root, 0.3)
    checks.append(
        (
            "静音点加入手动列表",
            len(app.manual_cuts) == 2
            and all(any(s <= cut <= e for s, e in app.silences) for cut in app.manual_cuts),
        )
    )
    print(f"  手动分割点={app.manual_cuts}")
    shot13 = grab(root, "13_smart_split.png")
    print(f"  截图:{shot13}")

    # 端到端:按静音/呼吸切分输出 3 段
    out_s = os.path.join(tmp.name, "speech_split")
    app.outdir_var.set(out_s)
    app.prefix_var.set("sp_")
    app._refresh_preview()
    print("按静音/呼吸切分…")
    app._start_split()
    wait_until(root, lambda: not app._busy, 120, "智能分句分割完成")
    pump(root, 0.4)
    s_files = sorted(os.listdir(out_s)) if os.path.isdir(out_s) else []
    checks.append(("按静音切分输出 3 段", len(s_files) == 3))
    print(f"  智能分句输出:{s_files}")

    app._on_close()
    tmp.cleanup()

    print("\n===== 冒烟测试结果 =====")
    failed = 0
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        failed += 0 if passed else 1
    print(f"  合计:{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
