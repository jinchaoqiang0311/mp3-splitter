"""MP3 分割工具图形界面(tkinter)。

界面结构:
    - 顶部:打开文件 + 文件信息
    - 中部:波形图(点击定位 / 双击添加分割点 / 拖动红线微调 / 右键删除)
    - 下方:试听控制条、自动分割设置、手动分割点列表、输出设置
    - 底部:开始分割 + 进度
"""

from __future__ import annotations

import math
import os
import queue
import shutil
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

try:  # 拖放支持(可选依赖,未安装时自动降级为无拖放)
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺少 tkinterdnd2 时
    DND_FILES = None
    TkinterDnD = None
    DND_AVAILABLE = False

from .audio_engine import (
    AudioEngineError,
    AudioInfo,
    Segment,
    build_segments,
    demucs_available,
    detect_silences,
    extract_waveform,
    make_preview_proxy,
    probe,
    separate_vocals,
    silence_cut_points,
    split_file,
)
from .player import MciError, MciPlayer
from .timecode import fmt_clock, parse_time

APP_TITLE = "MP3 分割工具"

# 配色
CANVAS_BG = "#ffffff"
GRID_COLOR = "#e5e9f0"
WAVE_FILL = "#9ec5fe"
WAVE_LINE = "#3b82f6"
PLAYHEAD_COLOR = "#0f172a"
CUT_COLOR = "#dc2626"
CUT_INACTIVE_COLOR = "#f3b0b0"
AUTO_CUT_COLOR = "#94a3b8"
REDUNDANCY_COLOR = "#fdf0c8"
# 智能分句:检测到的静音/呼吸区间标注色
SILENCE_COLOR = "#e3f6e6"
MUTED = "#64748b"

# 波形区边框颜色(默认 / 拖拽悬停高亮)
WAVE_BORDER = "#cbd5e1"

# 拖放/选择载入时允许的扩展名(音频 + 可从视频容器中提取音轨)
SUPPORTED_EXTENSIONS = {
    # 音频
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma",
    # 视频容器(载入时自动提取音轨)
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv",
    ".3gp", ".ts", ".mka",
}

# 波形最小可视窗口(秒)
MIN_VIEW_SPAN = 1.0

PRESETS = [("60 秒", 60), ("180 秒", 180), ("300 秒", 300), ("600 秒", 600)]

# 智能分句模式(内部值 -> 界面显示文案)
SMART_MODE_LABELS = {"snap": "时长+吸附", "silence": "纯静音切分"}

# 静音/呼吸检测灵敏度(档位 -> (噪声阈值 dB, 最短静音秒))。
# 阈值越高(-30dB 高于 -45dB)越容易把轻微停顿/呼吸识别为静音;最短静音越短越易捕捉短促呼吸。
SENSITIVITY_PRESETS = {
    "宽松": (-30.0, 0.25),
    "标准": (-35.0, 0.35),
    "严格": (-45.0, 0.50),
}
DEFAULT_SENSITIVITY = "宽松"

# 试听分割点时前后各播放的秒数
AUDITION_PAD = 3.0


class Mp3SplitterApp:
    """应用主窗口。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.info: AudioInfo | None = None
        self.peaks: list[float] = []
        self.manual_cuts: list[float] = []
        self.segments: list[Segment] = []        # 最终分段(含冗余)
        self.base_segments: list[Segment] = []   # 不含冗余的基础分段(用于显示切割位置)

        self.playhead = 0.0
        self._playing = False
        self._busy = False
        self._drag_index: int | None = None
        self._redraw_pending = False
        self._edit_entry: ttk.Entry | None = None
        self._edit_index = -1

        # 音源(当前文件 / 分离结果)
        self.origin_path: str | None = None
        self.vocal_path: str | None = None
        self.inst_path: str | None = None
        self._loading_as_source = False

        # 拖放载入
        self.dnd_ready = False       # 拖放是否注册成功
        self._drag_status = ""       # 拖拽悬停前的状态栏文本

        # 波形可视时间窗口(秒)
        self.view_start = 0.0
        self.view_span = 1.0

        self._wave_top = 14
        self._wave_bottom = 160
        self._ph_line: int | None = None
        self._ph_tri: int | None = None

        self._player = MciPlayer()
        self._player_ready = False
        self._preview_proxy: str | None = None   # 试听副本(内置播放器不支持原格式时生成)
        self._preview_pending = False            # 正在生成试听副本
        self._closing = False                    # 窗口正在关闭

        # 智能分句(静音/呼吸检测)
        self.silences: list[tuple[float, float]] = []
        self._silences_path: str | None = None         # 已完成检测的文件
        self._silence_pending_path: str | None = None  # 正在检测的文件
        self._silence_seq = 0                          # 检测序号:换文件或改灵敏度时作废旧结果
        self._last_out_dir = ""
        # 后台线程通过队列投递结果,由主线程轮询处理(线程安全)
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self._build_vars()
        self._build_ui()
        self._update_smart_controls()
        self._setup_dnd()
        self._set_ready_state(False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._tick)

    # ================================================================= UI 构建
    def _build_vars(self) -> None:
        self.auto_enabled = tk.BooleanVar(value=True)
        self.chunk_var = tk.StringVar(value="300")
        self.merge_tail = tk.BooleanVar(value=True)
        self.redundancy_var = tk.StringVar(value="0.0")
        self.follow_playhead = tk.BooleanVar(value=True)
        self.lossless_var = tk.BooleanVar(value=True)
        self.outdir_var = tk.StringVar()
        self.prefix_var = tk.StringVar()
        self.status_var = tk.StringVar(value="请先打开一个 MP3 文件,也可把音频/视频文件拖入窗口。")
        self.preview_var = tk.StringVar(value="")
        self.pos_var = tk.StringVar(value="00:00.0 / 00:00.0")
        self.engine_var = tk.StringVar(value="light")
        self.engine_hint_var = tk.StringVar(value="")
        # 智能分句(静音/呼吸检测)
        self.smart_enabled = tk.BooleanVar(value=True)
        self.smart_mode_label = tk.StringVar(value=SMART_MODE_LABELS["snap"])
        self.snap_range_var = tk.StringVar(value="5.0")
        self.sensitivity_label = tk.StringVar(value=DEFAULT_SENSITIVITY)
        self.silence_hint_var = tk.StringVar(value="")

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        self._build_toolbar()
        self._build_wave()
        self._build_transport()
        self._build_settings()
        self._build_output()
        self._build_separation()
        self._build_bottom()

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        bar.grid(row=0, column=0, sticky="ew")
        self.open_btn = ttk.Button(bar, text="打开音频文件…", command=self._open_file)
        self.open_btn.pack(side="left")
        self.file_label = ttk.Label(bar, text="尚未选择文件", foreground=MUTED)
        self.file_label.pack(side="left", padx=(12, 0))

    def _build_wave(self) -> None:
        wrap = ttk.Frame(self.root, padding=(12, 4, 12, 0))
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            wrap, height=200, bg=CANVAS_BG, highlightthickness=1,
            highlightbackground=WAVE_BORDER, cursor="arrow",
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.hscroll = ttk.Scrollbar(wrap, orient="horizontal", command=self._on_hscroll)
        self.hscroll.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", self._on_wave_press)
        self.canvas.bind("<B1-Motion>", self._on_wave_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_wave_release)
        self.canvas.bind("<Double-Button-1>", self._on_wave_double)
        self.canvas.bind("<Button-3>", self._on_wave_right)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_wheel_shift)

    def _build_transport(self) -> None:
        bar = ttk.Frame(self.root, padding=(12, 6, 12, 0))
        bar.grid(row=2, column=0, sticky="ew")
        self.play_btn = ttk.Button(bar, text="播放", width=9, command=self._toggle_play)
        self.play_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="停止", width=7, command=self._stop_play)
        self.stop_btn.pack(side="left", padx=(6, 12))
        ttk.Label(bar, textvariable=self.pos_var, foreground=MUTED).pack(side="left")

        ttk.Checkbutton(bar, text="跟随播放", variable=self.follow_playhead).pack(side="right")
        ttk.Button(bar, text="全览", width=6, command=self._zoom_reset).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="+", width=3, command=lambda: self._zoom(0.8)).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="-", width=3, command=lambda: self._zoom(1.25)).pack(side="right", padx=(4, 0))
        ttk.Label(bar, text="缩放:", foreground=MUTED).pack(side="right", padx=(12, 4))
        ttk.Label(
            bar, text="滚轮缩放 · 双击添加分割点 · 右键删除", foreground=MUTED
        ).pack(side="right", padx=(0, 14))

    def _build_settings(self) -> None:
        area = ttk.Frame(self.root, padding=(12, 8, 12, 0))
        area.grid(row=3, column=0, sticky="ew")
        area.columnconfigure(0, weight=1, uniform="col")
        area.columnconfigure(1, weight=1, uniform="col")

        # -------- 自动分割
        auto = ttk.LabelFrame(area, text="自动分割", padding=(10, 8))
        auto.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        row = ttk.Frame(auto)
        row.pack(fill="x")
        ttk.Checkbutton(
            row, text="每段时长", variable=self.auto_enabled, command=self._refresh_preview
        ).pack(side="left")
        self.chunk_spin = ttk.Spinbox(
            row, textvariable=self.chunk_var, from_=1, to=86400, increment=1, width=10,
        )
        self.chunk_spin.pack(side="left", padx=(6, 4))
        ttk.Label(row, text="秒(可输小数)", foreground=MUTED).pack(side="left")
        self.chunk_var.trace_add("write", lambda *_: self._refresh_preview())

        row2 = ttk.Frame(auto)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="快捷:", foreground=MUTED).pack(side="left")
        for label, seconds in PRESETS:
            ttk.Button(
                row2, text=label, width=8,
                command=lambda s=seconds: self._apply_preset(s),
            ).pack(side="left", padx=2)

        smart = ttk.Frame(auto)
        smart.pack(fill="x", pady=(6, 0))
        self.smart_check = ttk.Checkbutton(
            smart, text="智能分句", variable=self.smart_enabled,
            command=self._on_smart_toggle,
        )
        self.smart_check.pack(side="left")
        self.smart_mode_box = ttk.Combobox(
            smart, state="readonly", width=12, textvariable=self.smart_mode_label,
            values=[SMART_MODE_LABELS["snap"], SMART_MODE_LABELS["silence"]],
        )
        self.smart_mode_box.pack(side="left", padx=(6, 0))
        self.smart_mode_box.bind("<<ComboboxSelected>>", lambda _e: self._on_smart_change())
        ttk.Label(smart, text="灵敏度", foreground=MUTED).pack(side="left", padx=(10, 2))
        self.sens_box = ttk.Combobox(
            smart, state="readonly", width=7, textvariable=self.sensitivity_label,
            values=list(SENSITIVITY_PRESETS),
        )
        self.sens_box.pack(side="left")
        self.sens_box.bind("<<ComboboxSelected>>", lambda _e: self._on_sensitivity_change())

        smart2 = ttk.Frame(auto)
        smart2.pack(fill="x", pady=(4, 0))
        ttk.Label(smart2, text="吸附范围 ±", foreground=MUTED).pack(side="left")
        self.snap_spin = ttk.Spinbox(
            smart2, textvariable=self.snap_range_var, from_=0.1, to=60.0,
            increment=0.5, width=6, command=self._on_smart_change,
        )
        self.snap_spin.pack(side="left", padx=(4, 2))
        ttk.Label(smart2, text="秒", foreground=MUTED).pack(side="left")
        ttk.Label(smart2, textvariable=self.silence_hint_var, foreground=MUTED).pack(
            side="left", padx=(8, 0)
        )
        self.snap_range_var.trace_add("write", lambda *_: self._refresh_preview())

        ttk.Checkbutton(
            auto, text="最后不足一段并入前一段", variable=self.merge_tail,
            command=self._refresh_preview,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            auto, textvariable=self.preview_var, foreground="#334155", wraplength=420,
        ).pack(anchor="w", pady=(6, 0))

        # -------- 手动分割点
        manual = ttk.LabelFrame(area, text="手动分割点", padding=(10, 8))
        manual.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        manual.columnconfigure(0, weight=1)
        manual.rowconfigure(0, weight=1)

        list_wrap = ttk.Frame(manual)
        list_wrap.grid(row=0, column=0, sticky="nsew")
        list_wrap.columnconfigure(0, weight=1)
        list_wrap.rowconfigure(0, weight=1)
        self.cut_list = tk.Listbox(
            list_wrap, height=5, activestyle="dotbox", exportselection=False,
            highlightthickness=1, highlightbackground="#cbd5e1",
        )
        self.cut_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_wrap, command=self.cut_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.cut_list.configure(yscrollcommand=scroll.set)
        self.cut_list.bind("<<ListboxSelect>>", self._on_cut_list_select)
        self.cut_list.bind("<Double-Button-1>", self._on_cut_list_double)

        btns = ttk.Frame(manual)
        btns.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="在播放头添加", width=13, command=self._add_cut_at_playhead).pack(side="left")
        ttk.Button(btns, text="删除选中", width=10, command=self._remove_selected_cut).pack(side="left", padx=4)
        ttk.Button(btns, text="清空", width=7, command=self._clear_cuts).pack(side="left")
        self.audition_btn = ttk.Button(
            btns, text="试听所选 ±3 秒", width=15, command=self._audition_selected
        )
        self.audition_btn.pack(side="left", padx=(4, 0))

        btns2 = ttk.Frame(manual)
        btns2.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.silence_cut_btn = ttk.Button(
            btns2, text="静音点加入列表", width=14, command=self._add_silence_cuts
        )
        self.silence_cut_btn.pack(side="left")
        ttk.Label(
            btns2, text="把智能分句检测到的静音/呼吸处批量加入", foreground=MUTED
        ).pack(side="left", padx=(6, 0))

        ttk.Label(
            manual,
            text="单击列表定位播放头;双击行内时间可直接编辑(支持 90.5 或 1:30.5)。",
            foreground=MUTED, wraplength=420,
        ).grid(row=3, column=0, sticky="w", pady=(4, 0))

    def _build_output(self) -> None:
        box = ttk.LabelFrame(self.root, text="输出设置", padding=(10, 8))
        box.grid(row=4, column=0, sticky="ew", padx=12, pady=(8, 0))
        box.columnconfigure(1, weight=1)

        row0 = ttk.Frame(box)
        row0.grid(row=0, column=0, columnspan=4, sticky="ew")
        ttk.Label(row0, text="分段冗余").pack(side="left")
        self.redundancy_spin = tk.Spinbox(
            row0, from_=0.0, to=600.0, increment=0.1, format="%.1f", width=8,
            justify="right", textvariable=self.redundancy_var,
            command=self._refresh_preview,
        )
        self.redundancy_spin.pack(side="left", padx=8)
        ttk.Label(
            row0, text="秒(每段首尾各延长该时长,与相邻段重叠;0 表示不冗余)",
            foreground=MUTED,
        ).pack(side="left")
        self.redundancy_var.trace_add("write", lambda *_: self._refresh_preview())

        ttk.Label(box, text="输出目录").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.outdir_var).grid(
            row=1, column=1, sticky="ew", padx=8, pady=(8, 0)
        )
        ttk.Button(box, text="浏览…", width=8, command=self._choose_outdir).grid(
            row=1, column=2, pady=(8, 0)
        )
        ttk.Button(box, text="打开", width=6, command=self._open_outdir).grid(
            row=1, column=3, padx=(4, 0), pady=(8, 0)
        )

        row = ttk.Frame(box)
        row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(row, text="文件名前缀").pack(side="left")
        ttk.Entry(row, textvariable=self.prefix_var, width=22).pack(side="left", padx=8)
        self.lossless_check = ttk.Checkbutton(
            row, text="无损分割(直接复制音频流,不重编码、速度快)", variable=self.lossless_var
        )
        self.lossless_check.pack(side="left", padx=(10, 0))

    def _build_separation(self) -> None:
        box = ttk.LabelFrame(self.root, text="人声 / 伴奏分离", padding=(10, 8))
        box.grid(row=5, column=0, sticky="ew", padx=12, pady=(8, 0))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.separate_btn = ttk.Button(
            row, text="分离并保存…", width=14, command=self._separate
        )
        self.separate_btn.pack(side="left")

        self.demucs_ready = demucs_available()
        self.engine_ai_btn = ttk.Radiobutton(
            row,
            text="AI 高质量(强力去背景音)",
            variable=self.engine_var,
            value="ai",
            command=self._on_engine_change,
        )
        self.engine_ai_btn.pack(side="left", padx=(12, 0))
        if not self.demucs_ready:
            self.engine_ai_btn.state(["disabled"])
        ttk.Radiobutton(
            row,
            text="快速(中置声道,数秒)",
            variable=self.engine_var,
            value="light",
            command=self._on_engine_change,
        ).pack(side="left", padx=(8, 0))

        self.source_btns: dict[str, ttk.Button] = {}
        for key, label in (("inst", "伴奏"), ("vocals", "人声"), ("origin", "原曲")):
            btn = ttk.Button(
                row, text=label, width=6, command=lambda k=key: self._load_source(k)
            )
            btn.pack(side="right", padx=2)
            self.source_btns[key] = btn
        ttk.Label(row, text="载入音源:", foreground=MUTED).pack(side="right", padx=(12, 4))

        hint = ttk.Frame(box)
        hint.pack(fill="x", pady=(4, 0))
        ttk.Label(hint, textvariable=self.engine_hint_var, foreground=MUTED).pack(
            side="left"
        )

        self.engine_var.set("ai" if self.demucs_ready else "light")
        self._on_engine_change()

    def _on_engine_change(self) -> None:
        if self.engine_var.get() == "ai":
            self.engine_hint_var.set(
                "AI 模式:最大限度提取人声并抑制背景音乐;首次运行自动下载模型(约 80 MB),CPU 处理较慢,请耐心等待"
            )
        elif self.demucs_ready:
            self.engine_hint_var.set(
                "快速模式:数秒完成,但人声会保留居中乐器与部分背景音(仅限立体声)"
            )
        else:
            self.engine_hint_var.set(
                "未检测到 Demucs,仅可使用快速模式;需 AI 纯人声分离请执行: pip install demucs"
            )

    def _build_bottom(self) -> None:
        bottom = ttk.Frame(self.root, padding=(12, 10, 12, 12))
        bottom.grid(row=6, column=0, sticky="ew")
        bottom.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(bottom, text="开始分割", command=self._start_split)
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=1, sticky="ew", padx=12)
        ttk.Label(bottom, textvariable=self.status_var, foreground="#334155").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

    # ================================================================= 状态
    def _set_ready_state(self, ready: bool) -> None:
        state = ["!disabled"] if ready else ["disabled"]
        self.play_btn.state(state if self._player_ready else ["disabled"])
        self.stop_btn.state(state if self._player_ready else ["disabled"])
        self.audition_btn.state(state if self._player_ready else ["disabled"])
        self._update_start_state()

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    # ================================================================= 拖放载入
    def _setup_dnd(self) -> None:
        """给窗口内所有控件注册文件拖放,拖入音频/视频即可载入。"""
        if not DND_AVAILABLE:
            self.status_var.set("请先打开一个 MP3 文件。")
            return
        stack = [self.root]
        registered = 0
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DropEnter>>", self._on_drag_enter)
                widget.dnd_bind("<<DropLeave>>", self._on_drag_leave)
            except Exception:
                continue
            registered += 1
        self.dnd_ready = registered > 0
        if not self.dnd_ready:
            self.status_var.set("请先打开一个 MP3 文件。")

    def _parse_drop_paths(self, data: str) -> list[str]:
        """解析拖放数据:tkdnd 给出 Tcl 列表文本,含空格的路径由 {} 包裹。"""
        try:
            items = list(self.root.tk.splitlist(data))
        except Exception:
            items = [data]
        # 个别后端会直接给出未转义的 Windows 路径(反斜杠会被 Tcl 解析吞掉),
        # 检测到这种情况且原文本确实是文件时,按原文本处理。
        if (
            len(items) == 1
            and "\\" not in items[0]
            and "\\" in data
            and os.path.isfile(data)
        ):
            items = [data]
        return [item for item in items if item]

    def _on_drag_enter(self, event) -> None:
        """文件被拖到窗口上方:高亮波形区并提示松开载入。"""
        if self._busy or self._drag_status:
            return
        self._drag_status = self.status_var.get()
        self._set_status("松开鼠标即可载入音频/视频文件…")
        self.canvas.configure(highlightbackground=WAVE_LINE, highlightthickness=2)

    def _on_drag_leave(self, event) -> None:
        self._reset_drag_hint()

    def _reset_drag_hint(self) -> None:
        """恢复拖拽悬停前的边框与状态提示。"""
        self.canvas.configure(highlightbackground=WAVE_BORDER, highlightthickness=1)
        if self._drag_status:
            self._set_status(self._drag_status)
            self._drag_status = ""

    def _on_drop(self, event) -> None:
        """文件被拖入:取第一个音频/视频文件载入。"""
        self._reset_drag_hint()
        if self._busy:
            self._set_status("正在处理中,请稍候再拖入文件。")
            return
        paths = self._parse_drop_paths(event.data)
        if not paths:
            return
        path = os.path.abspath(paths[0])
        if not os.path.isfile(path):
            messagebox.showwarning(APP_TITLE, f"无法识别拖入的内容:\n{paths[0]}")
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            messagebox.showwarning(
                APP_TITLE,
                f"不支持的文件类型:{ext or os.path.basename(path)}\n"
                "请拖入音频或视频文件(mp3 / wav / flac / m4a / aac / ogg / wma /\n"
                "mp4 / mkv / webm / mov / avi / wmv / flv / 3gp / ts / mka)。",
            )
            return
        self._load_file(path)

    # ================================================================= 加载文件
    def _open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择要分割的音频/视频文件",
            filetypes=[
                ("MP3 文件", "*.mp3"),
                ("音频文件", "*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.wma"),
                ("视频文件(提取音轨)", "*.mp4 *.mkv *.webm *.mov *.avi *.wmv *.flv *.3gp *.ts *.mka"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self._load_file(path)

    def _load_file(self, path: str, *, as_source: bool = False) -> None:
        """加载音频文件;``as_source=True`` 表示切换分离音源(不改写原始文件记录)。"""
        if self._busy:
            return
        self._loading_as_source = as_source
        self._busy = True
        self.open_btn.state(["disabled"])
        self._update_start_state()
        self._set_status("正在解析音频,请稍候…")
        self.file_label.configure(text=os.path.basename(path) + "  解析中…")
        threading.Thread(target=self._load_worker, args=(path,), daemon=True).start()

    def _load_worker(self, path: str) -> None:
        try:
            info = probe(path)
            peaks = extract_waveform(path)
        except Exception as exc:  # noqa: BLE001 - 线程内统一兜底
            self._queue.put(("load_failed", str(exc)))
        else:
            self._queue.put(("load_done", (info, peaks)))

    def _load_failed(self, message: str) -> None:
        self._busy = False
        self.open_btn.state(["!disabled"])
        self.file_label.configure(text="尚未选择文件")
        self._update_start_state()
        self._set_status("加载失败。")
        messagebox.showerror(APP_TITLE, f"无法加载音频:\n{message}")

    def _load_done(self, info: AudioInfo, peaks: list[float]) -> None:
        self._busy = False
        self.open_btn.state(["!disabled"])
        if not self._loading_as_source:
            self.origin_path = info.path
            self.vocal_path = None
            self.inst_path = None
        self.info = info
        self.peaks = peaks
        self.manual_cuts = []
        self.playhead = 0.0
        self._playing = False
        self.view_start = 0.0
        self.view_span = max(MIN_VIEW_SPAN, info.duration)

        details = []
        if info.bitrate_kbps:
            details.append(f"{info.bitrate_kbps} kbps")
        if info.sample_rate:
            details.append(f"{info.sample_rate} Hz")
        if info.channels:
            details.append("立体声" if info.channels >= 2 else "单声道")
        details.append(f"{info.size_mb:.1f} MB")
        text = f"{os.path.basename(info.path)}   时长 {fmt_clock(info.duration)}"
        if details:
            text += "   ·   " + "   ·   ".join(details)
        self.file_label.configure(text=text)

        stem = os.path.splitext(os.path.basename(info.path))[0]
        default_dir = os.path.join(os.path.dirname(os.path.abspath(info.path)), stem + "_split")
        self.outdir_var.set(default_dir)
        self.prefix_var.set(stem + "_part")
        self._last_out_dir = default_dir

        is_mp3 = info.path.lower().endswith(".mp3")
        self.lossless_check.state(["!disabled"] if is_mp3 else ["disabled"])
        if not is_mp3:
            self.lossless_var.set(False)

        self._open_player(info.path)
        # 切换文件后重新检测静音/呼吸点(用于智能分句)
        self._silence_seq += 1  # 作废上一个文件仍在进行中的检测
        self.silences = []
        self._silences_path = None
        self._ensure_silences()
        self._refresh_cut_list()
        self._refresh_preview()
        self._redraw_wave()
        self._sync_scrollbar()
        self._update_pos_label()
        self._update_source_buttons()
        if self._preview_pending:
            self._set_status("内置播放器不支持该格式,正在生成试听副本(不影响分割)…")
        else:
            self._set_status("文件已就绪,设置好分割方式后点击“开始分割”。")

    def _open_player(self, path: str) -> None:
        """打开内置试听;播放器不支持该格式时,后台生成 MP3 试听副本。"""
        self._player_ready = False
        self._preview_pending = False
        self._discard_preview_proxy()
        if not self._player.available:
            self._set_status("当前系统不支持内置试听,分割功能不受影响。")
            self._set_ready_state(True)
            return
        try:
            self._player.open(path)
        except MciError:
            self._start_preview_proxy(path)
        else:
            self._player_ready = True
        self._set_ready_state(True)

    def _start_preview_proxy(self, path: str) -> None:
        """内置播放器打不开原格式时,后台转一份 MP3 试听副本。"""
        self._preview_pending = True
        threading.Thread(target=self._preview_worker, args=(path,), daemon=True).start()

    def _preview_worker(self, path: str) -> None:
        try:
            proxy = make_preview_proxy(path)
        except Exception as exc:  # noqa: BLE001 - 线程内统一兜底
            self._queue.put(("preview_failed", (path, str(exc))))
        else:
            self._queue.put(("preview_done", (path, proxy)))

    def _preview_done(self, path: str, proxy: str) -> None:
        """试听副本生成完毕:仍是当前文件时用它打开播放器。"""
        self._preview_pending = False
        if self._closing or not self._is_current(path):
            shutil.rmtree(os.path.dirname(proxy), ignore_errors=True)
            return
        self._preview_proxy = proxy
        try:
            self._player.open(proxy)
        except MciError:
            self._set_status("内置试听不可用,分割功能不受影响。")
            return
        self._player_ready = True
        self._set_ready_state(True)
        self._set_status("试听副本已就绪:内置播放器不支持该格式,已自动转码。")

    def _preview_failed(self, path: str, message: str) -> None:
        self._preview_pending = False
        if self._closing or not self._is_current(path):
            return
        self._set_status("内置试听不可用,分割功能不受影响。")

    def _is_current(self, path: str) -> bool:
        """判断路径是否仍是当前载入的文件。"""
        if self.info is None:
            return False
        return os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(self.info.path)
        )

    def _discard_preview_proxy(self) -> None:
        """删除已生成的试听副本。"""
        if self._preview_proxy:
            shutil.rmtree(os.path.dirname(self._preview_proxy), ignore_errors=True)
            self._preview_proxy = None

    # ================================================================= 智能分句
    def _smart_mode(self) -> str:
        """当前智能分句模式:snap(时长+吸附)或 silence(纯静音切分)。"""
        if self.smart_mode_label.get() == SMART_MODE_LABELS["silence"]:
            return "silence"
        return "snap"

    def _snap_range(self) -> float:
        """吸附搜索范围(±秒)。"""
        text = self.snap_range_var.get().strip()
        try:
            value = float(text)
        except ValueError:
            return 0.0
        return max(0.0, min(value, 120.0))

    def _silences_ready(self) -> bool:
        """当前文件的静音/呼吸点是否已检测完成。"""
        if not self.info or self._silences_path is None:
            return False
        return os.path.normcase(os.path.abspath(self._silences_path)) == os.path.normcase(
            os.path.abspath(self.info.path)
        )

    def _silence_pending_current(self) -> bool:
        """是否正在为当前文件检测静音/呼吸点。"""
        if not self.info or not self._silence_pending_path:
            return False
        return os.path.normcase(os.path.abspath(self._silence_pending_path)) == os.path.normcase(
            os.path.abspath(self.info.path)
        )

    def _on_smart_toggle(self) -> None:
        self._update_smart_controls()
        self._ensure_silences()
        self._refresh_preview()
        self._redraw_wave()

    def _on_smart_change(self) -> None:
        self._update_smart_controls()
        self._ensure_silences()
        self._refresh_preview()
        self._redraw_wave()

    def _update_smart_controls(self) -> None:
        """按开关与模式刷新智能分句控件的可用状态。"""
        on = self.smart_enabled.get()
        mode = self._smart_mode() if on else None
        self.smart_mode_box.configure(state="readonly" if on else "disabled")
        self.sens_box.configure(state="readonly" if on else "disabled")
        self.snap_spin.configure(state="normal" if mode == "snap" else "disabled")
        self.chunk_spin.configure(state="disabled" if mode == "silence" else "normal")
        self._update_silence_hint()

    def _update_silence_hint(self) -> None:
        if not self.smart_enabled.get():
            self.silence_hint_var.set("")
        elif self._silence_pending_current():
            self.silence_hint_var.set("正在检测…")
        elif self._silences_ready():
            self.silence_hint_var.set(f"已检测到 {len(self.silences)} 处静音/呼吸点")
        else:
            self.silence_hint_var.set("")

    def _silence_params(self) -> tuple[float, float]:
        """当前灵敏度档位对应的 (噪声阈值 dB, 最短静音秒)。"""
        return SENSITIVITY_PRESETS.get(
            self.sensitivity_label.get(), SENSITIVITY_PRESETS[DEFAULT_SENSITIVITY]
        )

    def _on_sensitivity_change(self) -> None:
        """灵敏度变化:作废已有结果并按新档位重新检测。"""
        self._silence_seq += 1        # 让进行中的旧检测结果失效
        self.silences = []
        self._silences_path = None
        self._silence_pending_path = None
        self._ensure_silences()
        self._update_silence_hint()
        self._refresh_preview()
        self._redraw_wave()

    def _ensure_silences(self) -> None:
        """智能分句开启且当前文件尚未检测时,后台启动静音/呼吸检测。"""
        if not self.info or not self.smart_enabled.get():
            return
        if self._silences_ready() or self._silence_pending_current():
            return
        path = self.info.path
        noise_db, min_silence = self._silence_params()
        self._silence_pending_path = path
        self._silence_seq += 1
        self.silence_hint_var.set("正在检测…")
        threading.Thread(
            target=self._silence_worker,
            args=(path, self.info.duration, noise_db, min_silence, self._silence_seq),
            daemon=True,
        ).start()

    def _silence_worker(
        self, path: str, duration: float, noise_db: float, min_silence: float, seq: int
    ) -> None:
        try:
            silences = detect_silences(
                path, duration=duration, noise_db=noise_db, min_silence=min_silence
            )
        except Exception as exc:  # noqa: BLE001 - 线程内统一兜底
            self._queue.put(("silence_failed", (path, str(exc), seq)))
        else:
            self._queue.put(("silence_done", (path, silences, seq)))

    def _silence_done(
        self, path: str, silences: list[tuple[float, float]], seq: int
    ) -> None:
        if seq != self._silence_seq:
            return  # 灵敏度已变更或已切换文件,丢弃过期结果
        self._silence_pending_path = None
        if self._closing or not self._is_current(path):
            return
        self.silences = [tuple(item) for item in silences]
        self._silences_path = path
        self._update_silence_hint()
        self._refresh_preview()
        self._redraw_wave()

    def _silence_failed(self, path: str, message: str, seq: int) -> None:
        if seq != self._silence_seq:
            return
        self._silence_pending_path = None
        if self._closing or not self._is_current(path):
            return
        self.silences = []
        self._silences_path = None
        self.silence_hint_var.set("静音检测失败,智能分句暂不可用")

    def _add_silence_cuts(self) -> None:
        """把检测到的静音/呼吸中点批量加入手动分割点。"""
        if not self.info:
            return
        if not self._silences_ready():
            if not self.smart_enabled.get():
                self._set_status("请先勾选「智能分句」以检测静音/呼吸点。")
            elif self._silence_pending_current():
                self._set_status("静音/呼吸点还在检测中,请稍候。")
            else:
                self._set_status("尚无静音/呼吸点可用。")
            return
        duration = self.info.duration
        added = 0
        for start, end in self.silences:
            mid = round((start + end) / 2, 3)
            if mid < 0.05 or mid > duration - 0.05:
                continue
            if any(abs(cut - mid) < 0.1 for cut in self.manual_cuts):
                continue
            self.manual_cuts.append(mid)
            added += 1
        if not added:
            self._set_status("静音点已全部在列表中,没有新增。")
            return
        self.manual_cuts.sort()
        self._refresh_cut_list()
        self._refresh_preview()
        self._redraw_wave()
        self._set_status(f"已加入 {added} 个静音/呼吸点到手动分割点。")

    def _update_source_buttons(self) -> None:
        """按当前加载状态刷新「载入音源」按钮。"""
        ready = bool(self.info) and not self._busy
        available = {
            "origin": self.origin_path,
            "vocals": self.vocal_path,
            "inst": self.inst_path,
        }
        for key, btn in self.source_btns.items():
            path = available[key]
            enabled = ready and bool(path) and os.path.isfile(path)
            btn.state(["!disabled"] if enabled else ["disabled"])

    def _load_source(self, key: str) -> None:
        """把分离结果或原始文件重新载入为当前音源。"""
        path = {
            "origin": self.origin_path,
            "vocals": self.vocal_path,
            "inst": self.inst_path,
        }.get(key)
        if path and os.path.isfile(path):
            self._load_file(path, as_source=True)

    # ================================================================= 播放
    def _toggle_play(self) -> None:
        if not self._player_ready:
            return
        if self._playing:
            self._player.pause()
            self._playing = False
            self.play_btn.configure(text="继续")
        else:
            self._player.play(int(self.playhead * 1000))
            self._playing = True
            self.play_btn.configure(text="暂停")

    def _stop_play(self) -> None:
        if not self._player_ready:
            return
        self._player.stop()
        self._playing = False
        self.play_btn.configure(text="播放")
        self._set_playhead(0.0, follow=False)

    def _tick(self) -> None:
        """定时刷新播放进度,并处理后台线程投递的消息。"""
        try:
            self._drain_queue()
            if self._player_ready and self._playing and self.info:
                pos = self._player.position_ms() / 1000.0
                mode = self._player.mode()
                if mode == "stopped" or pos >= self.info.duration - 0.2:
                    self._playing = False
                    self.play_btn.configure(text="播放")
                    pos = min(pos, self.info.duration)
                self.playhead = max(0.0, min(pos, self.info.duration))
                self._follow_view()
                self._update_playhead_marker()
                self._update_pos_label()
        finally:
            self.root.after(100, self._tick)

    def _drain_queue(self) -> None:
        """在主线程中处理后台线程投递的事件。"""
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            if kind == "load_done":
                self._load_done(*payload)
            elif kind == "load_failed":
                self._load_failed(payload)
            elif kind == "split_progress":
                self._on_progress(*payload)
            elif kind == "split_done":
                self._split_done(*payload)
            elif kind == "split_failed":
                self._split_failed(payload)
            elif kind == "sep_progress":
                self._sep_progress(*payload)
            elif kind == "sep_done":
                self._sep_done(*payload)
            elif kind == "sep_failed":
                self._sep_failed(payload)
            elif kind == "preview_done":
                self._preview_done(*payload)
            elif kind == "preview_failed":
                self._preview_failed(*payload)
            elif kind == "silence_done":
                self._silence_done(*payload)
            elif kind == "silence_failed":
                self._silence_failed(*payload)

    def _follow_view(self) -> None:
        """播放时若播放头移出可视窗口,自动滚动跟随。"""
        if not self.info or not self.follow_playhead.get():
            return
        if self.view_span >= self.info.duration - 1e-6:
            return
        if not self._time_visible(self.playhead):
            self._set_view(self.playhead - self.view_span / 2)

    def _update_pos_label(self) -> None:
        duration = self.info.duration if self.info else 0.0
        self.pos_var.set(f"{fmt_clock(self.playhead, ms=True)} / {fmt_clock(duration, ms=True)}")

    def _set_playhead(self, seconds: float, *, follow: bool = True) -> None:
        if not self.info:
            return
        self.playhead = max(0.0, min(seconds, self.info.duration))
        self._update_playhead_marker()
        self._update_pos_label()
        if follow and self._playing and self._player_ready:
            self._player.play(int(self.playhead * 1000))

    # ================================================================= 波形绘制
    def _on_canvas_configure(self, _event=None) -> None:
        if self._redraw_pending:
            return
        self._redraw_pending = True
        self.root.after(30, self._do_redraw)

    def _do_redraw(self) -> None:
        self._redraw_pending = False
        self._redraw_wave()

    def _time_to_x(self, seconds: float) -> float:
        width = max(1, self.canvas.winfo_width())
        if not self.info or self.view_span <= 0:
            return 0.0
        return (seconds - self.view_start) / self.view_span * width

    def _x_to_time(self, x: float) -> float:
        width = max(1, self.canvas.winfo_width())
        if not self.info:
            return 0.0
        value = self.view_start + x / width * self.view_span
        return max(0.0, min(value, self.info.duration))

    def _time_visible(self, seconds: float, margin: float = 0.0) -> bool:
        return self.view_start - margin <= seconds <= self.view_start + self.view_span + margin

    # ================================================================= 视图缩放
    def _set_view(self, start: float, span: float | None = None) -> None:
        """设置波形的可视时间窗口(秒)。"""
        if not self.info:
            return
        duration = self.info.duration
        span = self.view_span if span is None else span
        span = max(MIN_VIEW_SPAN, min(span, duration))
        start = max(0.0, min(start, duration - span))
        changed = abs(start - self.view_start) > 1e-9 or abs(span - self.view_span) > 1e-9
        self.view_start, self.view_span = start, span
        self._sync_scrollbar()
        if changed:
            self._redraw_wave()

    def _sync_scrollbar(self) -> None:
        if not self.info or self.info.duration <= 0:
            self.hscroll.set(0.0, 1.0)
            return
        first = self.view_start / self.info.duration
        last = (self.view_start + self.view_span) / self.info.duration
        self.hscroll.set(first, last)

    def _zoom(self, factor: float, anchor_x: float | None = None) -> None:
        """以画布锚点像素位置为中心缩放(factor<1 放大,>1 缩小)。"""
        if not self.info:
            return
        width = max(1, self.canvas.winfo_width())
        if anchor_x is None:
            anchor_x = width / 2
        ratio = min(1.0, max(0.0, anchor_x / width))
        anchor_time = self._x_to_time(anchor_x)
        new_span = max(MIN_VIEW_SPAN, min(self.view_span * factor, self.info.duration))
        self._set_view(anchor_time - ratio * new_span, new_span)

    def _zoom_reset(self) -> None:
        if self.info:
            self._set_view(0.0, self.info.duration)

    def _on_hscroll(self, *args) -> None:
        if not self.info:
            return
        if args and args[0] == "moveto":
            self._set_view(float(args[1]) * self.info.duration)
        elif args and args[0] == "scroll":
            amount = float(args[1])
            step = 0.9 if len(args) > 2 and args[2] == "pages" else 0.1
            self._set_view(self.view_start + amount * self.view_span * step)

    def _on_wheel(self, event) -> None:
        if not self.info:
            return
        factor = 0.8 if event.delta > 0 else 1.25
        self._zoom(factor, event.x)

    def _on_wheel_shift(self, event) -> None:
        if not self.info:
            return
        step = self.view_span * 0.15
        self._set_view(self.view_start + (-step if event.delta > 0 else step))

    def _redraw_wave(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 4 or height <= 4:
            return
        self._wave_top = 16
        self._wave_bottom = height - 24

        self._draw_ruler(width)
        self._draw_redundancy(width)
        self._draw_silences(width)
        if self.peaks and self.info:
            mid = (self._wave_top + self._wave_bottom) / 2
            half = max(4.0, (self._wave_bottom - self._wave_top) / 2 - 2)
            # 按峰值归一化显示(最大放大 3 倍),让低音量素材的波形也清晰可辨
            peak_max = max(self.peaks)
            gain = 1.0 if peak_max <= 1e-6 else min(1.0 / peak_max, 3.0)
            count = len(self.peaks)
            duration = self.info.duration
            top_pts: list[tuple[float, float]] = []
            bottom_pts: list[tuple[float, float]] = []
            for x in range(width):
                t0 = self.view_start + x / width * self.view_span
                t1 = self.view_start + (x + 1) / width * self.view_span
                i0 = int(t0 / duration * count)
                i1 = int(t1 / duration * count)
                i0 = max(0, min(i0, count - 1))
                i1 = max(i0 + 1, min(i1, count))
                peak = max(self.peaks[i0:i1])
                y = max(0.6, min(1.0, peak * gain) * half)
                top_pts.append((x, mid - y))
                bottom_pts.append((x, mid + y))
            coords: list[float] = []
            for x, y in top_pts:
                coords.extend((x, y))
            for x, y in reversed(bottom_pts):
                coords.extend((x, y))
            canvas.create_polygon(coords, fill=WAVE_FILL, outline=WAVE_LINE, width=1, tags="wave")
        elif not self.info:
            canvas.create_text(
                width / 2, (self._wave_top + self._wave_bottom) / 2,
                text="打开一个音频文件后,这里会显示波形", fill=MUTED,
            )

        self._draw_auto_cuts(width)
        self._draw_manual_cuts(width)
        self._draw_playhead()

    def _draw_redundancy(self, width: int) -> None:
        """把相邻两段的重叠区(冗余部分)画成浅色带。"""
        canvas = self.canvas
        canvas.delete("redundancy")
        segments = self.segments or []
        for index in range(len(segments) - 1):
            left, right = segments[index], segments[index + 1]
            start, end = right.start, left.end
            if end - start <= 1e-6:
                continue
            x0 = self._time_to_x(start)
            x1 = self._time_to_x(end)
            if x1 < -4 or x0 > width + 4:
                continue
            canvas.create_rectangle(
                x0, self._wave_top, x1, self._wave_bottom,
                fill=REDUNDANCY_COLOR, outline="", tags="redundancy",
            )

    def _draw_silences(self, width: int) -> None:
        """把检测到的静音/呼吸区间画成浅绿色带,便于确认切点是否落在句尾。"""
        canvas = self.canvas
        canvas.delete("silence")
        if not self.info or not self.smart_enabled.get() or not self._silences_ready():
            return
        for start, end in self.silences:
            x0 = self._time_to_x(start)
            x1 = self._time_to_x(end)
            if x1 < -4 or x0 > width + 4:
                continue
            canvas.create_rectangle(
                x0, self._wave_top, x1, self._wave_bottom,
                fill=SILENCE_COLOR, outline="", tags="silence",
            )

    def _pick_ruler_step(self, span: float, width: int) -> float:
        candidates = [
            0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30,
            60, 120, 300, 600, 900, 1800, 3600, 7200, 14400,
        ]
        for step in candidates:
            if step / max(span, 1e-6) * width >= 80:
                return step
        return candidates[-1]

    def _draw_ruler(self, width: int) -> None:
        canvas = self.canvas
        canvas.create_line(0, self._wave_bottom, width, self._wave_bottom, fill=GRID_COLOR)
        if not self.info:
            return
        step = self._pick_ruler_step(self.view_span, width)
        tick = math.floor(self.view_start / step) * step
        end = self.view_start + self.view_span
        while tick <= end + 1e-6:
            x = self._time_to_x(tick)
            if -40 <= x <= width + 40:
                canvas.create_line(x, self._wave_bottom, x, self._wave_bottom + 4, fill="#94a3b8")
                label = f"{tick:.1f}s" if step < 1 else fmt_clock(tick)
                canvas.create_text(
                    x + 3, self._wave_bottom + 12, text=label, anchor="nw",
                    fill="#94a3b8", font=("Segoe UI", 8),
                )
            tick += step

    def _draw_auto_cuts(self, width: int | None = None) -> None:
        self.canvas.delete("autocut")
        if not self.info or not self.segments:
            return
        width = width if width is not None else self.canvas.winfo_width()
        manual = {round(t, 3) for t in self.manual_cuts}
        for seg in self.base_segments or self.segments:
            end = seg.end
            if end >= self.info.duration - 0.01 or round(end, 3) in manual:
                continue
            x = self._time_to_x(end)
            if x < -4 or x > width + 4:
                continue
            self.canvas.create_line(
                x, self._wave_top, x, self._wave_bottom,
                fill=AUTO_CUT_COLOR, dash=(3, 4), tags="autocut",
            )

    def _draw_manual_cuts(self, width: int | None = None) -> None:
        """绘制手动分割点:已生效的为红色实线,因并入末段等原因未生效的为浅红虚线。"""
        self.canvas.delete("cut")
        self._cut_line_ids: list[int] = []
        width = width if width is not None else self.canvas.winfo_width()
        boundaries: set[float] = set()
        for seg in self.base_segments or self.segments:
            boundaries.add(round(seg.start, 3))
            boundaries.add(round(seg.end, 3))
        for cut in self.manual_cuts:
            x = self._time_to_x(cut)
            if x < -6 or x > width + 6:
                self._cut_line_ids.append(-1)
                continue
            active = round(cut, 3) in boundaries
            if active:
                line = self.canvas.create_line(
                    x, self._wave_top, x, self._wave_bottom,
                    fill=CUT_COLOR, width=2, tags="cut",
                )
            else:
                line = self.canvas.create_line(
                    x, self._wave_top, x, self._wave_bottom,
                    fill=CUT_INACTIVE_COLOR, width=2, dash=(4, 3), tags="cut",
                )
            self.canvas.create_polygon(
                x - 5, self._wave_top - 8, x + 5, self._wave_top - 8, x, self._wave_top,
                fill=CUT_COLOR if active else CUT_INACTIVE_COLOR, outline="", tags="cut",
            )
            self._cut_line_ids.append(line)

    def _draw_playhead(self) -> None:
        if not self.info or not self._time_visible(self.playhead):
            self._ph_line = self._ph_tri = None
            return
        x = self._time_to_x(self.playhead)
        self._ph_line = self.canvas.create_line(
            x, self._wave_top, x, self._wave_bottom,
            fill=PLAYHEAD_COLOR, width=2, tags="playhead",
        )
        self._ph_tri = self.canvas.create_polygon(
            x - 5, self._wave_top - 8, x + 5, self._wave_top - 8, x, self._wave_top - 1,
            fill=PLAYHEAD_COLOR, outline="", tags="playhead",
        )

    def _update_playhead_marker(self) -> None:
        if not self.info:
            return
        if not self._time_visible(self.playhead):
            if self._ph_line or self._ph_tri:
                self.canvas.delete("playhead")
                self._ph_line = self._ph_tri = None
            return
        if not self._ph_line or not self.canvas.type(self._ph_line):
            self._draw_playhead()
            return
        x = self._time_to_x(self.playhead)
        self.canvas.coords(self._ph_line, x, self._wave_top, x, self._wave_bottom)
        self.canvas.coords(
            self._ph_tri, x - 5, self._wave_top - 8, x + 5, self._wave_top - 8, x, self._wave_top - 1
        )

    # ================================================================= 波形交互
    def _hit_cut(self, x: float, tol: float = 6.0) -> int | None:
        best: int | None = None
        best_dist = tol + 1
        for index, cut in enumerate(self.manual_cuts):
            dist = abs(self._time_to_x(cut) - x)
            if dist <= tol and dist < best_dist:
                best, best_dist = index, dist
        return best

    def _on_wave_press(self, event) -> None:
        if not self.info:
            return
        index = self._hit_cut(event.x)
        if index is not None:
            self._drag_index = index
            self.canvas.configure(cursor="sb_h_double_arrow")
            self.cut_list.selection_clear(0, "end")
            self.cut_list.selection_set(index)
            self.cut_list.see(index)
        else:
            self._drag_index = None
            self._set_playhead(self._x_to_time(event.x))

    def _on_wave_drag(self, event) -> None:
        if self._drag_index is None or not self.info:
            return
        value = self._x_to_time(event.x)
        value = max(0.05, min(value, self.info.duration - 0.05))
        self.manual_cuts[self._drag_index] = value
        line_ids = getattr(self, "_cut_line_ids", [])
        if self._drag_index < len(line_ids):
            line_id = line_ids[self._drag_index]
            if line_id != -1 and self.canvas.type(line_id):
                x = self._time_to_x(value)
                self.canvas.coords(line_id, x, self._wave_top, x, self._wave_bottom)

    def _on_wave_release(self, _event) -> None:
        if self._drag_index is None:
            return
        self._drag_index = None
        self.canvas.configure(cursor="arrow")
        self._refresh_cut_list()
        self._refresh_preview()
        self._redraw_wave()

    def _on_wave_double(self, event) -> None:
        if not self.info:
            return
        self._add_cut(self._x_to_time(event.x))

    def _on_wave_right(self, event) -> None:
        index = self._hit_cut(event.x)
        if index is None:
            return
        del self.manual_cuts[index]
        self._refresh_cut_list()
        self._refresh_preview()
        self._redraw_wave()
        self._set_status("已删除分割点。")

    # ================================================================= 分割点管理
    def _add_cut(self, seconds: float) -> None:
        if not self.info:
            return
        value = max(0.1, min(seconds, self.info.duration - 0.1))
        if any(abs(c - value) < 0.15 for c in self.manual_cuts):
            self._set_status("该位置附近已有分割点。")
            return
        self.manual_cuts.append(value)
        self.manual_cuts.sort()
        self._refresh_cut_list()
        self._refresh_preview()
        self._redraw_wave()
        self._set_status(f"已添加分割点 {fmt_clock(value, ms=True)}。")

    def _add_cut_at_playhead(self) -> None:
        if not self.info:
            return
        self._add_cut(self.playhead)

    def _remove_selected_cut(self) -> None:
        selection = self.cut_list.curselection()
        if not selection:
            self._set_status("请先在列表中选择一个分割点。")
            return
        del self.manual_cuts[selection[0]]
        self._refresh_cut_list()
        self._refresh_preview()
        self._redraw_wave()

    def _clear_cuts(self) -> None:
        if not self.manual_cuts:
            return
        self.manual_cuts.clear()
        self._refresh_cut_list()
        self._refresh_preview()
        self._redraw_wave()
        self._set_status("已清空手动分割点。")

    def _refresh_cut_list(self) -> None:
        self.cut_list.delete(0, "end")
        for index, cut in enumerate(self.manual_cuts, 1):
            self.cut_list.insert("end", f"{index:>2}.  {fmt_clock(cut, ms=True)}")

    def _on_cut_list_select(self, _event) -> None:
        """单击(或键盘选中)列表项:播放头跳到该分割点。"""
        if self._edit_entry is not None:
            return
        selection = self.cut_list.curselection()
        if selection and self.info:
            index = selection[0]
            if index < len(self.manual_cuts):
                self._set_playhead(self.manual_cuts[index], follow=False)
                self._redraw_wave()

    def _on_cut_list_double(self, _event) -> None:
        self._begin_edit_cut()

    def _begin_edit_cut(self) -> None:
        """在列表行内放置输入框,就地编辑分割点时间。"""
        if not self.info or self._edit_entry is not None:
            return
        selection = self.cut_list.curselection()
        if not selection:
            return
        index = selection[0]
        bbox = self.cut_list.bbox(index)
        if not bbox:
            return
        x, y, box_width, box_height = bbox
        entry = ttk.Entry(self.cut_list)
        entry.insert(0, fmt_clock(self.manual_cuts[index], ms=True))
        entry.select_range(0, "end")
        entry.place(x=x, y=y, width=box_width, height=box_height)
        entry.focus_set()
        self._edit_entry = entry
        self._edit_index = index
        entry.bind("<Return>", lambda e: self._commit_edit_cut())
        entry.bind("<Escape>", lambda e: self._cancel_edit_cut())
        entry.bind("<FocusOut>", lambda e: self._commit_edit_cut())

    def _commit_edit_cut(self) -> None:
        entry = self._edit_entry
        if entry is None:
            return
        text = entry.get()
        index = self._edit_index
        self._edit_entry = None
        self._edit_index = -1
        entry.destroy()
        if not self.info or index < 0 or index >= len(self.manual_cuts):
            return
        value = parse_time(text)
        if value is None:
            self._set_status("时间格式无法识别,已取消编辑。")
            return
        value = round(max(0.05, min(value, self.info.duration - 0.05)), 3)
        others = [c for i, c in enumerate(self.manual_cuts) if i != index]
        if any(abs(c - value) < 0.1 for c in others):
            self._set_status("与其它分割点过近,已取消编辑。")
            return
        self.manual_cuts[index] = value
        self.manual_cuts.sort()
        self._refresh_cut_list()
        new_index = self.manual_cuts.index(value)
        self.cut_list.selection_clear(0, "end")
        self.cut_list.selection_set(new_index)
        self._refresh_preview()
        self._redraw_wave()
        self._set_status(f"分割点已更新为 {fmt_clock(value, ms=True)}。")

    def _cancel_edit_cut(self) -> None:
        entry = self._edit_entry
        if entry is None:
            return
        self._edit_entry = None
        self._edit_index = -1
        entry.destroy()

    def _audition_selected(self) -> None:
        if not self.info or not self._player_ready:
            return
        selection = self.cut_list.curselection()
        if selection:
            index = selection[0]
        else:
            index = self._nearest_cut_index(self.playhead)
            if index is None:
                self._set_status("列表中没有分割点。")
                return
        cut = self.manual_cuts[index]
        start = max(0.0, cut - AUDITION_PAD)
        end = min(self.info.duration, cut + AUDITION_PAD)
        self._player.play(int(start * 1000), int(end * 1000))
        self._playing = True
        self.play_btn.configure(text="暂停")
        self._set_playhead(start, follow=False)

    def _nearest_cut_index(self, seconds: float) -> int | None:
        if not self.manual_cuts:
            return None
        return min(range(len(self.manual_cuts)), key=lambda i: abs(self.manual_cuts[i] - seconds))

    # ================================================================= 分段预览
    def _current_chunk(self) -> float | None:
        """自动分段时长(单位:秒,保留一位小数);输入非法时返回 None。"""
        if not self.auto_enabled.get():
            return None
        text = self.chunk_var.get().strip()
        if not text:
            return None
        value: float | None = None
        try:
            value = float(text)
        except ValueError:
            value = parse_time(text)  # 容错:也接受 5:00 这类写法
        if value is None or value <= 0:
            return None
        return round(value, 1)

    def _redundancy(self) -> float:
        """分段冗余时长(单位:秒,保留一位小数)。"""
        text = self.redundancy_var.get().strip()
        if not text:
            return 0.0
        try:
            value = float(text)
        except ValueError:
            return 0.0
        return max(0.0, round(min(value, 600.0), 1))

    def _build_segments(self, *, redundancy: float = 0.0) -> list[Segment]:
        """按当前设置(含智能分句)生成分段。"""
        if not self.info:
            return []
        smart = self._smart_mode() if self.smart_enabled.get() else None
        silences: list[tuple[float, float]] = []
        if smart and self._silences_ready():
            silences = self.silences
        cuts = list(self.manual_cuts)
        chunk = self._current_chunk()
        if smart == "silence":
            cuts += silence_cut_points(self.info.duration, silences)
            chunk = None
        return build_segments(
            self.info.duration,
            cuts,
            chunk,
            merge_tail=self.merge_tail.get(),
            redundancy=redundancy,
            silences=silences if smart == "snap" else None,
            snap_tolerance=self._snap_range() if smart == "snap" else 0.0,
        )

    def _compute_segments(self, redundancy: float | None = None) -> list[Segment]:
        if redundancy is None:
            redundancy = self._redundancy()
        return self._build_segments(redundancy=redundancy)

    def _refresh_preview(self) -> None:
        if not self.info:
            self.preview_var.set("打开文件后可预览分段结果。")
            return
        chunk = self._current_chunk()
        raw_input = self.chunk_var.get().strip()
        smart = self._smart_mode() if self.smart_enabled.get() else None
        if smart != "silence" and self.auto_enabled.get() and chunk is None:
            self.preview_var.set(f'无法识别"{raw_input}",请输入大于 0 的秒数,例如 300 或 90.5。')
            return
        redundancy = self._redundancy()
        base = self._build_segments()
        self.base_segments = base
        self.segments = self._compute_segments(redundancy)
        if len(self.segments) <= 1:
            self.preview_var.set("当前设置不会产生切割点(整个文件将作为 1 段输出)。")
        else:
            if smart == "silence":
                head = "智能分句:在静音/呼吸处切分"
            elif chunk:
                head = f"每段 {chunk:g} 秒自动切分"
                if smart == "snap" and self._silences_ready():
                    head += f",切点吸附 ±{self._snap_range():g} 秒内最近静音"
            else:
                head = "按手动分割点切分"
            if len(base) <= 6:
                detail = " + ".join(fmt_clock(s.duration) for s in base)
                text = f"{head}:共 {len(base)} 段({detail})"
            else:
                text = (
                    f"{head}:共 {len(base)} 段,"
                    f"首段 {fmt_clock(base[0].duration)},末段 {fmt_clock(base[-1].duration)}"
                )
            if redundancy > 0:
                text += f";冗余 ±{redundancy:.1f} 秒(每段已相应延长)"
                if base and min(s.duration for s in base) < 2 * redundancy:
                    text += ",部分段已按段长限制"
            self.preview_var.set(text)
        width = self.canvas.winfo_width()
        self._draw_redundancy(width)
        self._draw_silences(width)
        self._draw_auto_cuts(width)
        self._draw_manual_cuts(width)
        self._update_start_state()

    def _update_start_state(self) -> None:
        ready = bool(self.info) and not self._busy
        self.start_btn.state(["!disabled"] if ready else ["disabled"])
        self.separate_btn.state(["!disabled"] if ready else ["disabled"])
        self._update_source_buttons()

    def _apply_preset(self, seconds: int) -> None:
        self.auto_enabled.set(True)
        self.chunk_var.set(str(seconds))
        self._refresh_preview()

    # ================================================================= 输出设置
    def _choose_outdir(self) -> None:
        initial = self.outdir_var.get() or os.path.expanduser("~")
        path = filedialog.askdirectory(title="选择输出目录", initialdir=initial)
        if path:
            self.outdir_var.set(path)
            self._last_out_dir = path

    def _open_outdir(self) -> None:
        target = self.outdir_var.get().strip() or self._last_out_dir
        if not target:
            return
        if not os.path.isdir(target):
            messagebox.showinfo(APP_TITLE, "输出目录尚不存在。")
            return
        try:
            os.startfile(target)  # noqa: S606 - Windows 上打开资源管理器
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"无法打开目录:{exc}")

    # ================================================================= 人声分离
    def _separate(self) -> None:
        """选择保存目录并启动人声/伴奏分离。"""
        if self._busy or not self.info:
            return
        engine = self.engine_var.get()
        if engine == "ai" and not self.demucs_ready:
            messagebox.showwarning(APP_TITLE, "未检测到 Demucs,无法使用 AI 模式。")
            return
        if engine == "light" and self.info.channels and self.info.channels < 2:
            messagebox.showwarning(
                APP_TITLE,
                "快速模式仅支持立体声文件。请改用 AI 高质量模式,或换用立体声音源。",
            )
            return
        stem = os.path.splitext(os.path.basename(self.info.path))[0]
        default = os.path.join(
            os.path.dirname(os.path.abspath(self.info.path)), stem + "_分离"
        )
        out_dir = filedialog.askdirectory(
            title="选择分离文件的保存目录", initialdir=default, mustexist=False
        )
        if out_dir:
            self._start_separation(out_dir, engine)

    def _start_separation(self, out_dir: str, engine: str | None = None) -> None:
        if self._busy or not self.info:
            return
        engine = engine or self.engine_var.get()
        self._busy = True
        self.open_btn.state(["disabled"])
        self._update_start_state()
        self.progress.configure(maximum=2, value=0)
        self._set_status("正在分离人声与伴奏…")
        stem = os.path.splitext(os.path.basename(self.info.path))[0]
        threading.Thread(
            target=self._separate_worker,
            args=(self.info.path, out_dir, stem, engine),
            daemon=True,
        ).start()

    def _separate_worker(
        self, src: str, out_dir: str, prefix: str, engine: str
    ) -> None:
        try:
            vocals, inst = separate_vocals(
                src,
                out_dir,
                prefix,
                engine=engine,
                progress_cb=lambda done, total, msg: self._queue.put(
                    ("sep_progress", (done, total, msg))
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 线程内统一兜底
            self._queue.put(("sep_failed", str(exc)))
        else:
            self._queue.put(("sep_done", (vocals, inst, out_dir)))

    def _sep_progress(self, done: int, total: int, label: str = "") -> None:
        self.progress.configure(maximum=total, value=done)
        text = f"正在分离…{label}" if label else "正在分离…"
        self._set_status(f"{text}({done}/{total})")

    def _sep_failed(self, message: str) -> None:
        self._busy = False
        self.open_btn.state(["!disabled"])
        self._update_start_state()
        self._set_status("分离失败。")
        messagebox.showerror(APP_TITLE, f"分离失败:\n{message}")

    def _sep_done(self, vocals: str, inst: str, out_dir: str) -> None:
        self._busy = False
        self.open_btn.state(["!disabled"])
        self.vocal_path = vocals
        self.inst_path = inst
        self._update_start_state()
        self.progress.configure(value=self.progress["maximum"])
        self._set_status(
            f"分离完成:{os.path.basename(vocals)} / {os.path.basename(inst)} → {out_dir}"
        )
        if messagebox.askyesno(
            APP_TITLE,
            "分离完成,已生成两个文件:\n"
            f"{os.path.basename(vocals)}\n{os.path.basename(inst)}\n\n"
            "可点击「载入音源」把其中任一个载入继续分割。\n是否打开保存目录?",
        ):
            try:
                os.startfile(out_dir)  # noqa: S606
            except OSError:
                pass

    # ================================================================= 执行分割
    def _start_split(self) -> None:
        if self._busy or not self.info:
            return
        chunk = self._current_chunk()
        if self.auto_enabled.get() and chunk is None:
            messagebox.showwarning(
                APP_TITLE, "每段时长无法识别,请输入大于 0 的秒数(可含一位小数)。"
            )
            return
        segments = self._compute_segments()
        self.segments = segments
        if len(segments) <= 1:
            if not messagebox.askyesno(
                APP_TITLE, "当前设置不会产生分割点,只会输出整个文件为 1 段。是否继续?"
            ):
                return
        out_dir = self.outdir_var.get().strip()
        if not out_dir:
            messagebox.showwarning(APP_TITLE, "请先设置输出目录。")
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"无法创建输出目录:\n{exc}")
            return

        prefix = self.prefix_var.get().strip() or (
            os.path.splitext(os.path.basename(self.info.path))[0] + "_part"
        )
        is_mp3 = self.info.path.lower().endswith(".mp3")
        reencode = not (is_mp3 and self.lossless_var.get())

        self._busy = True
        self.open_btn.state(["disabled"])
        self._update_start_state()
        self.progress.configure(maximum=len(segments), value=0)
        mode = "无损复制" if not reencode else "重编码"
        self._set_status(f"正在分割(共 {len(segments)} 段,{mode})…")
        threading.Thread(
            target=self._split_worker,
            args=(self.info, segments, out_dir, prefix, reencode),
            daemon=True,
        ).start()

    def _split_worker(self, info, segments, out_dir, prefix, reencode) -> None:
        try:
            outputs = split_file(
                info.path,
                segments,
                out_dir,
                prefix,
                reencode=reencode,
                progress_cb=lambda done, total, _msg: self._queue.put(
                    ("split_progress", (done, total))
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 线程内统一兜底
            self._queue.put(("split_failed", str(exc)))
        else:
            self._queue.put(("split_done", (outputs, out_dir)))

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.configure(maximum=total, value=done)
        self._set_status(f"正在分割…{done}/{total}")

    def _split_failed(self, message: str) -> None:
        self._busy = False
        self.open_btn.state(["!disabled"])
        self._update_start_state()
        self._set_status("分割失败。")
        messagebox.showerror(APP_TITLE, f"分割失败:\n{message}")

    def _split_done(self, outputs: list[str], out_dir: str) -> None:
        self._busy = False
        self.open_btn.state(["!disabled"])
        self._update_start_state()
        self._last_out_dir = out_dir
        self.progress.configure(value=self.progress["maximum"])
        self._set_status(f"完成:已输出 {len(outputs)} 个文件 → {out_dir}")
        if messagebox.askyesno(APP_TITLE, f"已生成 {len(outputs)} 个文件。\n是否打开输出目录?"):
            try:
                os.startfile(out_dir)  # noqa: S606
            except OSError:
                pass

    # ================================================================= 收尾
    def _on_close(self) -> None:
        self._closing = True
        try:
            self._player.close()
        finally:
            self._discard_preview_proxy()
            self.root.destroy()


def create_root() -> tk.Tk:
    """创建主窗口;安装了 tkinterdnd2 时返回支持拖放文件的主窗口。"""
    if DND_AVAILABLE:
        try:
            return TkinterDnD.Tk()
        except Exception:  # pragma: no cover - tkdnd 加载失败时优雅降级
            pass
    return tk.Tk()


def run() -> None:
    """创建窗口并进入事件循环。"""
    root = create_root()
    root.title(APP_TITLE)
    root.geometry("1080x880")
    root.minsize(920, 780)

    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    try:
        default_font = tkfont.nametofont("TkDefaultFont")
        if "Microsoft YaHei UI" in tkfont.families(root):
            default_font.configure(family="Microsoft YaHei UI")
    except Exception:
        pass
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass

    Mp3SplitterApp(root)
    root.mainloop()
