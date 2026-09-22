"""MP3 音频引擎:基于 ffmpeg 的时长探测、波形提取与分段切割。

本模块不依赖任何 GUI 框架,可单独导入用于脚本或自动化测试。
"""

from __future__ import annotations

import array
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

__all__ = [
    "AudioEngineError",
    "AudioInfo",
    "Segment",
    "ffmpeg_path",
    "probe",
    "extract_waveform",
    "make_preview_proxy",
    "detect_silences",
    "silence_cut_points",
    "build_segments",
    "split_file",
    "separate_vocals",
    "demucs_available",
]

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 小于该长度的片段将被丢弃(秒)
MIN_SEGMENT = 0.05
# 分割点之间的最小间隔(秒),过近的分割点会被合并
MIN_CUT_GAP = 0.1

# 静音/呼吸检测:判定阈值与最短静音时长(默认值)
SILENCE_NOISE_DB = -35.0
SILENCE_MIN_LEN = 0.35
# 纯静音切分模式:允许的最短段落(秒),更短的相邻段自动合并
SILENCE_MIN_SEGMENT = 1.0

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_RE = re.compile(r"Audio:\s*[\w\d]+.*?(\d+)\s*Hz,\s*([^\s,]+)")
_SILENCE_EVENT_RE = re.compile(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)")


class AudioEngineError(RuntimeError):
    """音频处理过程中的错误。"""


# ------------------------------------------------------------------ ffmpeg 定位
_ffmpeg_exe: Optional[str] = None


def ffmpeg_path() -> str:
    """返回可用的 ffmpeg 路径:优先系统安装,其次 imageio-ffmpeg 内置二进制。"""
    global _ffmpeg_exe
    if _ffmpeg_exe:
        return _ffmpeg_exe
    exe = shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg

            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:  # pragma: no cover - 环境缺失时的兜底
            raise AudioEngineError(
                "未找到 ffmpeg。请安装: pip install imageio-ffmpeg"
            ) from exc
    _ffmpeg_exe = exe
    return exe


def _run_capture(
    args: Sequence[str],
    *,
    check: bool = True,
    env: Optional[dict[str, str]] = None,
) -> tuple[int, bytes]:
    """执行命令,stderr 写入临时文件避免管道阻塞;check=True 时失败抛异常。"""
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(
            list(args),
            stdout=subprocess.DEVNULL,
            stderr=errf,
            creationflags=_CREATE_NO_WINDOW,
            env=env,
        )
        proc.wait()
        code = proc.returncode
        errf.seek(0)
        err = errf.read()
    if check and code != 0:
        tail = "\n".join(err.decode("utf-8", "replace").strip().splitlines()[-8:])
        raise AudioEngineError(f"ffmpeg 执行失败(退出码 {code}):\n{tail}")
    return code, err


# ------------------------------------------------------------------ 音频信息
@dataclass
class AudioInfo:
    """音频文件的基本信息。"""

    path: str
    duration: float          # 秒
    title: str = ""
    artist: str = ""
    bitrate_kbps: int = 0
    sample_rate: int = 0
    channels: int = 0
    size_bytes: int = 0

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1024 / 1024


def _first_tag(tags, *keys: str) -> str:
    if not tags:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if value:
            if isinstance(value, list):
                return str(value[0])
            return str(value)
    return ""


def _probe_with_mutagen(path: str) -> Optional[AudioInfo]:
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return None
    try:
        mf = MutagenFile(path)
    except Exception:
        return None
    if mf is None:
        return None
    info = getattr(mf, "info", None)
    length = float(getattr(info, "length", 0) or 0)
    if length <= 0:
        return None
    tags = getattr(mf, "tags", None)
    return AudioInfo(
        path=path,
        duration=length,
        title=_first_tag(tags, "TIT2", "title", "\xa9nam"),
        artist=_first_tag(tags, "TPE1", "artist", "\xa9ART"),
        bitrate_kbps=int((getattr(info, "bitrate", 0) or 0) // 1000),
        sample_rate=int(getattr(info, "sample_rate", 0) or 0),
        channels=int(getattr(info, "channels", 0) or 0),
        size_bytes=os.path.getsize(path),
    )


def _probe_with_ffmpeg(path: str) -> AudioInfo:
    code, err = _run_capture(
        [ffmpeg_path(), "-hide_banner", "-i", path], check=False
    )
    text = err.decode("utf-8", "replace")
    match = _DURATION_RE.search(text)
    if not match:
        raise AudioEngineError("无法解析音频信息,文件可能已损坏或格式不受支持。")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    bitrate = 0
    br_match = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if br_match:
        bitrate = int(br_match.group(1))
    sample_rate = channels = 0
    audio_match = _AUDIO_RE.search(text)
    if audio_match:
        sample_rate = int(audio_match.group(1))
        channel_text = audio_match.group(2)
        channels = {"stereo": 2, "mono": 1, "2.1": 3, "5.1": 6, "7.1": 8}.get(
            channel_text, 0
        )
    return AudioInfo(
        path=path,
        duration=duration,
        bitrate_kbps=bitrate,
        sample_rate=sample_rate,
        channels=channels,
        size_bytes=os.path.getsize(path),
    )


def probe(path: str) -> AudioInfo:
    """探测音频文件的时长与编码信息。"""
    if not os.path.isfile(path):
        raise AudioEngineError(f"文件不存在:{path}")
    info = _probe_with_mutagen(path)
    if info is None:
        info = _probe_with_ffmpeg(path)
    if info.duration <= 0:
        raise AudioEngineError("音频时长无效,无法处理。")
    return info


# ------------------------------------------------------------------ 波形提取
def extract_waveform(
    path: str, points: int = 3000, decode_rate: int = 8000
) -> list[float]:
    """把音频解码为低采样率单声道 PCM,降采样为 ``points`` 个 0~1 峰值。

    返回值可直接按画布宽度合并绘制成波形图。
    """
    args = [
        ffmpeg_path(), "-hide_banner", "-nostdin", "-i", path,
        "-vn", "-ac", "1", "-ar", str(decode_rate),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    samples = array.array("h")
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=errf,
            creationflags=_CREATE_NO_WINDOW,
        )
        carry = b""
        assert proc.stdout is not None
        while True:
            block = proc.stdout.read(1 << 20)
            if not block:
                break
            block = carry + block
            if len(block) & 1:  # 保证按 16bit 对齐
                carry, block = block[-1:], block[:-1]
            else:
                carry = b""
            if block:
                samples.frombytes(block)
        proc.stdout.close()
        proc.wait()
        if proc.returncode != 0 and len(samples) == 0:
            errf.seek(0)
            tail = errf.read().decode("utf-8", "replace").strip().splitlines()[-6:]
            raise AudioEngineError("解码音频失败:\n" + "\n".join(tail))

    total = len(samples)
    if total == 0:
        raise AudioEngineError("文件中未找到可解码的音频数据。")

    step = max(1, total // max(1, points))
    peaks: list[float] = []
    for start in range(0, total, step):
        chunk = samples[start:start + step]
        if not chunk:
            break
        low = min(chunk)
        high = max(chunk)
        peaks.append(min(1.0, max(high, -low) / 32768.0))
    return peaks


# ------------------------------------------------------------------ 试听副本
def make_preview_proxy(src: str, quality: int = 4) -> str:
    """把任意格式(含视频容器)转成内置播放器兼容的 MP3 试听副本。

    返回临时文件路径;副本位于独立的临时目录中,使用完毕可删除该目录。
    """
    ff = ffmpeg_path()
    tmp_dir = tempfile.mkdtemp(prefix="mp3split_preview_")
    dst = os.path.join(tmp_dir, "preview.mp3")
    args = [
        ff, "-hide_banner", "-nostdin", "-y", "-i", src,
        "-vn", "-map", "0:a:0",
        "-c:a", "libmp3lame", "-q:a", str(quality),
        "-id3v2_version", "3", dst,
    ]
    try:
        _run_capture(args)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return dst


# ------------------------------------------------------------------ 静音/呼吸检测
def detect_silences(
    src: str,
    *,
    noise_db: float = SILENCE_NOISE_DB,
    min_silence: float = SILENCE_MIN_LEN,
    duration: Optional[float] = None,
) -> list[tuple[float, float]]:
    """检测音频中的静音/呼吸间隙,返回按时间排序的 [(start, end), ...](秒)。

    ``noise_db`` 是判定阈值(越小越严格),``min_silence`` 是最短静音时长。
    ``duration`` 给出时,结尾处未闭合的静音按文件长度补齐。
    """
    args = [
        ffmpeg_path(), "-hide_banner", "-nostdin", "-i", src,
        "-vn", "-af", f"silencedetect=noise={noise_db:g}dB:d={min_silence:g}",
        "-f", "null", "-",
    ]
    _code, err = _run_capture(args)
    text = err.decode("utf-8", "replace")
    silences: list[tuple[float, float]] = []
    current: Optional[float] = None
    for kind, value in _SILENCE_EVENT_RE.findall(text):
        moment = max(0.0, float(value))
        if kind == "start":
            current = moment
        elif current is not None:
            if moment - current >= min_silence * 0.5:
                silences.append((round(current, 3), round(moment, 3)))
            current = None
    if current is not None and duration and duration > current:
        silences.append((round(current, 3), round(duration, 3)))
    return silences


def silence_cut_points(
    duration: float,
    silences: Sequence[tuple[float, float]],
    *,
    min_segment: float = SILENCE_MIN_SEGMENT,
) -> list[float]:
    """把静音区间中点转成切点;过滤掉会产生过短片段的切点。"""
    kept: list[float] = []
    last = 0.0
    for start, end in silences:
        mid = (start + end) / 2
        if mid - last < min_segment or duration - mid < min_segment:
            continue
        kept.append(round(mid, 3))
        last = mid
    return kept


def _snap_to_silence(
    point: float,
    silences: Sequence[tuple[float, float]],
    tolerance: float,
) -> Optional[float]:
    """在 ``tolerance`` 范围内找距离 ``point`` 最近的静音中点;找不到返回 None。"""
    best: Optional[tuple[float, float]] = None  # (距离, 中点)
    for start, end in silences:
        mid = (start + end) / 2
        delta = abs(mid - point)
        if delta <= tolerance and (best is None or delta < best[0]):
            best = (delta, mid)
    return None if best is None else round(best[1], 3)


# ------------------------------------------------------------------ 分段规划
@dataclass
class Segment:
    """一个待输出的片段(单位:秒)。"""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def build_segments(
    duration: float,
    cut_points: Optional[Iterable[float]] = None,
    chunk_seconds: Optional[float] = None,
    *,
    merge_tail: bool = False,
    redundancy: float = 0.0,
    silences: Optional[Sequence[tuple[float, float]]] = None,
    snap_tolerance: float = 0.0,
) -> list[Segment]:
    """根据自动分段时长与手动分割点,生成最终的分段列表。

    - ``chunk_seconds``:每隔该时长自动切一刀(None 或 <=0 表示不自动切)
    - ``cut_points``:手动分割点(秒);与自动分割点过近时自动合并
    - ``merge_tail``:最后一段不足 ``chunk_seconds`` 时并入前一段
    - ``redundancy``:每段首尾各延长的秒数(相邻段重叠,便于衔接);
      超过段长一半时会按段长自动限制;0 表示不延长
    - ``silences``/``snap_tolerance``:提供静音区间且容差 > 0 时,
      每个自动分割点会吸附到范围内最近的静音中点(避免切断句子/人声)
    """
    points: set[float] = set()
    if chunk_seconds and chunk_seconds > 0:
        count = int(duration // chunk_seconds)
        for k in range(1, count + 1):
            t = k * chunk_seconds
            if t < duration - MIN_SEGMENT:
                if silences and snap_tolerance > 0:
                    snapped = _snap_to_silence(t, silences, snap_tolerance)
                    if snapped is not None:
                        t = snapped
                points.add(round(t, 3))

    for point in cut_points or ():
        value = float(point)
        if MIN_SEGMENT <= value <= duration - MIN_SEGMENT:
            points.add(round(value, 3))

    ordered: list[float] = []
    for point in sorted(points):
        if not ordered or point - ordered[-1] >= MIN_CUT_GAP:
            ordered.append(point)

    bounds = [0.0, *ordered, duration]
    segments = [
        Segment(round(a, 3), round(b, 3))
        for a, b in zip(bounds, bounds[1:])
        if b - a >= MIN_SEGMENT
    ]
    if not segments:
        segments = [Segment(0.0, duration)]

    if merge_tail and chunk_seconds and chunk_seconds > 0 and len(segments) >= 2:
        if segments[-1].duration < chunk_seconds - MIN_SEGMENT:
            segments = [*segments[:-2], Segment(segments[-2].start, segments[-1].end)]

    if redundancy and redundancy > 0:
        padded: list[Segment] = []
        for seg in segments:
            pad = min(redundancy, seg.duration / 2)
            padded.append(
                Segment(
                    round(max(0.0, seg.start - pad), 3),
                    round(min(duration, seg.end + pad), 3),
                )
            )
        segments = padded

    return segments


# ------------------------------------------------------------------ 切割输出
def split_file(
    src: str,
    segments: Sequence[Segment],
    out_dir: str,
    prefix: str = "",
    *,
    reencode: bool = False,
    quality: int = 2,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> list[str]:
    """把 ``src`` 按 ``segments`` 依次切割到 ``out_dir``。

    ``reencode=False`` 时直接复制音频流(无损、快速);True 时用 libmp3lame 重编码。
    返回输出文件路径列表。
    """
    ff = ffmpeg_path()
    os.makedirs(out_dir, exist_ok=True)
    out_dir = os.path.abspath(out_dir)
    src_abs = os.path.abspath(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    prefix = prefix or f"{stem}_part"

    total = len(segments)
    width = max(2, len(str(total)))
    outputs: list[str] = []

    for index, seg in enumerate(segments, 1):
        if seg.duration < MIN_SEGMENT:
            continue
        dst = os.path.join(out_dir, f"{prefix}{index:0{width}d}.mp3")
        if os.path.abspath(dst) == src_abs:
            dst = os.path.join(out_dir, f"{prefix}{index:0{width}d}_out.mp3")
        args = [
            ff, "-hide_banner", "-nostdin", "-y",
            "-ss", f"{seg.start:.3f}",
            "-i", src,
            "-t", f"{seg.duration:.3f}",
            "-map", "0:a:0",
            "-map_metadata", "0",
        ]
        if reencode:
            args += ["-c:a", "libmp3lame", "-q:a", str(quality)]
        else:
            args += ["-c:a", "copy"]
        args += ["-id3v2_version", "3", dst]
        _run_capture(args)
        outputs.append(dst)
        if progress_cb:
            progress_cb(index, total, f"{index}/{total}")

    return outputs


# ------------------------------------------------------------------ 人声分离
DEMUCS_MODEL = "htdemucs"


def demucs_available() -> bool:
    """检测当前 Python 环境是否安装了 Demucs(AI 分离模型)。"""
    try:
        import importlib.util

        return importlib.util.find_spec("demucs") is not None
    except Exception:  # pragma: no cover - 极端环境兜底
        return False


def separate_vocals(
    src: str,
    out_dir: str,
    prefix: str = "",
    *,
    engine: str = "auto",
    quality: int = 2,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[str, str]:
    """把音频分离为「人声」与「伴奏」两个 MP3,返回 ``(人声文件, 伴奏文件)``。

    ``engine`` 可选:

    - ``"ai"``:用 Demucs 神经网络模型最大限度提取人声并抑制背景音乐
      (CPU 推理较慢,首次运行自动下载模型,需已安装 demucs)
    - ``"light"``:中置声道算法,数秒完成,但人声会保留居中乐器(仅立体声)
    - ``"auto"``:优先 AI,环境未安装 demucs 时回退 light
    """
    if engine in ("ai", "auto") and demucs_available():
        return _separate_with_demucs(src, out_dir, prefix, progress_cb=progress_cb)
    if engine == "ai":
        raise AudioEngineError(
            "未安装 Demucs,无法使用 AI 分离。安装命令: pip install demucs"
        )
    return _separate_by_center(
        src, out_dir, prefix, quality=quality, progress_cb=progress_cb
    )


def _separate_with_demucs(
    src: str,
    out_dir: str,
    prefix: str,
    *,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[str, str]:
    """用 Demucs 模型分离人声/伴奏并输出 MP3(CPU 推理,首次运行需下载模型)。"""
    src_abs = os.path.abspath(src)
    if not os.path.isfile(src_abs):
        raise AudioEngineError(f"文件不存在: {src}")
    os.makedirs(out_dir, exist_ok=True)
    out_dir = os.path.abspath(out_dir)
    stem = os.path.splitext(os.path.basename(src))[0]
    prefix = prefix or stem
    vocals_path = os.path.join(out_dir, f"{prefix}_vocals.mp3")
    inst_path = os.path.join(out_dir, f"{prefix}_instrumental.mp3")
    for dst in (vocals_path, inst_path):
        if os.path.abspath(dst) == src_abs:
            raise AudioEngineError("分离输出与源文件同名,请更换目录或文件名。")

    if progress_cb:
        progress_cb(0, 2, "AI 分离中,首次运行需下载模型,请耐心等待")
    with tempfile.TemporaryDirectory(prefix="mp3split_demucs_") as tmp:
        args = [
            sys.executable, "-m", "demucs",
            "--two-stems=vocals",
            "-n", DEMUCS_MODEL,
            "--mp3",
            "-o", tmp,
            src_abs,
        ]
        # demucs 4.x 从 HuggingFace 下载模型;默认走国内镜像,用户可用
        # 环境变量 HF_ENDPOINT 覆盖(已设置时不改动)。
        env = os.environ.copy()
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        code, err = _run_capture(args, check=False, env=env)
        if code != 0:
            tail = "\n".join(
                err.decode("utf-8", "replace").strip().splitlines()[-6:]
            )
            raise AudioEngineError(f"AI 分离失败(退出码 {code}):\n{tail}")

        found: dict[str, str] = {}
        for root, _dirs, files in os.walk(tmp):
            for name in files:
                key = os.path.splitext(name)[0].lower()
                if key in ("vocals", "no_vocals") and name.lower().endswith(".mp3"):
                    found[key] = os.path.join(root, name)
        if "vocals" not in found or "no_vocals" not in found:
            raise AudioEngineError("AI 分离未生成预期的输出文件,请重试。")
        if progress_cb:
            progress_cb(1, 2, "整理输出文件")
        shutil.move(found["vocals"], vocals_path)
        shutil.move(found["no_vocals"], inst_path)

    if progress_cb:
        progress_cb(2, 2, "完成")
    return vocals_path, inst_path


def _separate_by_center(
    src: str,
    out_dir: str,
    prefix: str = "",
    *,
    quality: int = 2,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[str, str]:
    """用中置声道算法把立体声音频分离为「人声」与「伴奏」两个 MP3。

    原理:人声通常位于立体声中央,左右声道相减可去掉人声得到伴奏;
    左右取平均则保留包括人声在内的中央成分。仅对立体声文件有效。
    返回 ``(人声文件, 伴奏文件)`` 路径。
    """
    info = probe(src)
    if info.channels < 2:
        raise AudioEngineError("该文件不是立体声,无法进行人声/伴奏分离。")
    ff = ffmpeg_path()
    os.makedirs(out_dir, exist_ok=True)
    out_dir = os.path.abspath(out_dir)
    stem = os.path.splitext(os.path.basename(src))[0]
    prefix = prefix or stem
    vocals_path = os.path.join(out_dir, f"{prefix}_vocals.mp3")
    inst_path = os.path.join(out_dir, f"{prefix}_instrumental.mp3")
    src_abs = os.path.abspath(src)
    for dst in (vocals_path, inst_path):
        if os.path.abspath(dst) == src_abs:
            raise AudioEngineError("分离输出与源文件同名,请更换目录或文件名。")

    jobs = [
        (
            vocals_path,
            "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1,"
            "highpass=f=90,lowpass=f=11000,volume=1.3",
            "人声",
        ),
        (
            inst_path,
            "pan=stereo|c0=c0-c1|c1=c1-c0,bass=g=5:f=130,volume=1.6",
            "伴奏",
        ),
    ]
    total = len(jobs)
    for index, (dst, filters, label) in enumerate(jobs, 1):
        _run_capture(
            [
                ff, "-hide_banner", "-nostdin", "-y", "-i", src,
                "-map", "0:a:0", "-af", filters,
                "-c:a", "libmp3lame", "-q:a", str(quality),
                "-id3v2_version", "3", dst,
            ]
        )
        if progress_cb:
            progress_cb(index, total, label)
    return vocals_path, inst_path
