"""MP3 分割工具包:自动按固定时长分段,或按手动标记的分割点切分。"""

from .audio_engine import (
    AudioEngineError,
    AudioInfo,
    Segment,
    build_segments,
    extract_waveform,
    ffmpeg_path,
    probe,
    split_file,
)
from .timecode import fmt_clock, parse_time

__version__ = "1.0.0"

__all__ = [
    "AudioEngineError",
    "AudioInfo",
    "Segment",
    "build_segments",
    "extract_waveform",
    "ffmpeg_path",
    "probe",
    "split_file",
    "fmt_clock",
    "parse_time",
    "__version__",
]
