"""音频引擎与时间码工具的自动化测试。

运行:
    python tests/test_engine.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mp3_splitter.audio_engine import (
    AudioEngineError,
    build_segments,
    demucs_available,
    detect_silences,
    extract_waveform,
    ffmpeg_path,
    probe,
    separate_vocals,
    silence_cut_points,
    split_file,
)
from mp3_splitter.timecode import fmt_clock, parse_time


def make_sample(path: str, seconds: float = 66.0, parts: int = 6) -> None:
    """生成由若干段不同频率正弦波拼接而成的测试 MP3。"""
    ff = ffmpeg_path()
    part = seconds / parts
    cmd = [ff, "-hide_banner", "-nostdin", "-y"]
    for index in range(parts):
        freq = 220 * (index + 1)
        rate = round(0.2 + 0.15 * index, 2)
        cmd += [
            "-f", "lavfi", "-i",
            f"sine=frequency={freq}:duration={part:.3f},tremolo=f={rate}:d=0.85",
        ]
    filters = "".join(f"[{i}:a]" for i in range(parts)) + f"concat=n={parts}:v=0:a=1"
    cmd += [
        "-filter_complex", filters, "-ar", "44100", "-ac", "2",
        "-c:a", "libmp3lame", "-b:a", "192k", path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def make_speech_like(
    path: str, *, burst: float = 1.5, gap: float = 0.8, bursts: int = 3
) -> None:
    """生成模拟“说话”的测试文件:若干段正弦音,句尾夹着静音间隙(呼吸)。"""
    ff = ffmpeg_path()
    cmd = [ff, "-hide_banner", "-nostdin", "-y"]
    for index in range(bursts):
        if index:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={gap:.3f}"]
        cmd += [
            "-f", "lavfi", "-i",
            f"sine=frequency=440:duration={burst:.3f}:sample_rate=44100",
        ]
    inputs = 2 * bursts - 1
    filters = "".join(f"[{i}:a]" for i in range(inputs)) + f"concat=n={inputs}:v=0:a=1"
    cmd += [
        "-filter_complex", filters, "-ar", "44100", "-ac", "2",
        "-c:a", "libmp3lame", "-b:a", "192k", path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


class TimecodeTest(unittest.TestCase):
    def test_parse_plain_seconds(self):
        self.assertEqual(parse_time("300"), 300.0)
        self.assertEqual(parse_time("90s"), 90.0)
        self.assertAlmostEqual(parse_time("1.5"), 1.5)

    def test_parse_units(self):
        self.assertEqual(parse_time("5m"), 300.0)
        self.assertEqual(parse_time("1.5h"), 5400.0)
        self.assertEqual(parse_time("5分30秒"), 330.0)
        self.assertEqual(parse_time("1小时2分"), 3720.0)

    def test_parse_colon_format(self):
        self.assertEqual(parse_time("5:00"), 300.0)
        self.assertEqual(parse_time("1:02:03"), 3723.0)
        self.assertAlmostEqual(parse_time("00:05.5"), 5.5)

    def test_parse_invalid(self):
        self.assertIsNone(parse_time("abc"))
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time("1:2:3:4"))

    def test_fmt_clock(self):
        self.assertEqual(fmt_clock(0), "00:00")
        self.assertEqual(fmt_clock(300), "05:00")
        self.assertEqual(fmt_clock(3723), "1:02:03")
        self.assertEqual(fmt_clock(83.456, ms=True), "01:23.456")


class SegmentPlanTest(unittest.TestCase):
    def test_auto_only(self):
        segments = build_segments(66.0, None, 15.0)
        self.assertEqual(len(segments), 5)
        self.assertAlmostEqual(segments[0].start, 0.0)
        self.assertAlmostEqual(segments[0].duration, 15.0)
        self.assertAlmostEqual(segments[-1].end, 66.0)
        self.assertAlmostEqual(segments[-1].duration, 6.0)

    def test_no_cuts_gives_single_segment(self):
        segments = build_segments(66.0, None, None)
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0].duration, 66.0)

    def test_manual_only(self):
        segments = build_segments(60.0, [10.0, 25.0, 40.0], None)
        self.assertEqual(len(segments), 4)
        self.assertAlmostEqual(segments[1].start, 10.0)
        self.assertAlmostEqual(segments[1].end, 25.0)

    def test_auto_and_manual_combined(self):
        segments = build_segments(60.0, [13.0], 20.0)
        self.assertEqual(len(segments), 4)
        self.assertAlmostEqual(segments[1].duration, 7.0)

    def test_close_points_merged(self):
        segments = build_segments(66.0, [15.0, 15.05], None)
        self.assertEqual(len(segments), 2)

    def test_out_of_range_points_ignored(self):
        segments = build_segments(60.0, [-5.0, 0.0, 60.0, 120.0], None)
        self.assertEqual(len(segments), 1)

    def test_merge_tail_flag(self):
        """merge_tail=True 时末段不足一段应并入前一段。"""
        segments = build_segments(66.0, None, 20.0, merge_tail=True)
        self.assertEqual(len(segments), 3)
        self.assertAlmostEqual(segments[-1].duration, 26.0)

    def test_redundancy_pads_both_ends(self):
        """冗余:每段首尾各延长,相邻段重叠 2×冗余。"""
        segments = build_segments(60.0, [20.0], None, redundancy=1.5)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0].start, 0.0)
        self.assertAlmostEqual(segments[0].end, 21.5)
        self.assertAlmostEqual(segments[1].start, 18.5)
        self.assertAlmostEqual(segments[1].end, 60.0)
        overlap = segments[0].end - segments[1].start
        self.assertAlmostEqual(overlap, 3.0)

    def test_redundancy_limited_by_segment_length(self):
        """冗余不会超过段长的一半。"""
        segments = build_segments(60.0, [10.0], None, redundancy=8.0)
        self.assertAlmostEqual(segments[0].start, 0.0)
        self.assertAlmostEqual(segments[0].end, 15.0)   # pad = min(8, 10/2) = 5
        self.assertAlmostEqual(segments[1].start, 2.0)  # pad = min(8, 50/2) = 8
        self.assertAlmostEqual(segments[1].end, 60.0)

    def test_redundancy_zero_keeps_segments(self):
        """冗余为 0 时保持原分段。"""
        plain = build_segments(60.0, [20.0], None)
        padded = build_segments(60.0, [20.0], None, redundancy=0.0)
        self.assertEqual(
            [(s.start, s.end) for s in plain],
            [(s.start, s.end) for s in padded],
        )

    def test_snap_auto_cut_to_silence(self):
        """智能分句:自动切点应吸附到范围内的静音中点。"""
        segments = build_segments(
            120.0, None, 60.0, silences=[(58.0, 60.0)], snap_tolerance=5.0
        )
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0].end, 59.0, places=3)

    def test_snap_ignores_far_silence(self):
        """静音中点超出吸附范围时,切点保持原位。"""
        segments = build_segments(
            120.0, None, 60.0, silences=[(30.0, 32.0)], snap_tolerance=5.0
        )
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0].end, 60.0, places=3)

    def test_silence_cut_points_filter_short(self):
        """纯静音切分:过短的相邻段会被过滤合并。"""
        cuts = silence_cut_points(
            30.0,
            [(5.0, 7.0), (6.5, 7.2), (20.0, 22.0), (29.5, 29.9)],
            min_segment=1.0,
        )
        self.assertEqual(cuts, [6.0, 21.0])


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sample = os.path.join(cls.tmp.name, "sample.mp3")
        make_sample(cls.sample, 66.0)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_probe(self):
        info = probe(self.sample)
        self.assertAlmostEqual(info.duration, 66.0, delta=0.6)
        self.assertEqual(info.sample_rate, 44100)
        self.assertEqual(info.channels, 2)
        self.assertGreater(info.size_bytes, 0)

    def test_waveform(self):
        peaks = extract_waveform(self.sample, points=500)
        self.assertGreater(len(peaks), 100)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in peaks))
        self.assertGreater(max(peaks), 0.05)

    def test_detect_silences(self):
        """静音/呼吸检测:应认出两处句尾静音,连续音则不应误报。"""
        speech = os.path.join(self.tmp.name, "speech.mp3")
        make_speech_like(speech)
        silences = detect_silences(speech, duration=probe(speech).duration)
        self.assertEqual(len(silences), 2)
        mids = [(a + b) / 2 for a, b in silences]
        self.assertLess(abs(mids[0] - 1.9), 0.25)
        self.assertLess(abs(mids[1] - 4.2), 0.25)
        # 连续正弦音不应误报(演示样本带深 tremolo,波谷本就近静音,不适合做基准)
        tone = os.path.join(self.tmp.name, "tone_cont.mp3")
        subprocess.run(
            [
                ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k", tone,
            ],
            check=True,
            capture_output=True,
        )
        self.assertEqual(detect_silences(tone), [])

    def test_detect_silences_sensitivity(self):
        """灵敏度:较响的轻呼吸(约 -37dB)只应被宽松阈值识别。"""
        quiet = os.path.join(self.tmp.name, "quiet_gap.mp3")
        # volume 经实测校准:0.02 实测约 -58dB(低于严格档 -45dB),
        # 提到 0.23 后约 -37dB,落在宽松阈值(-30dB)与严格阈值(-45dB)之间。
        subprocess.run(
            [
                ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8,volume=0.23",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
                "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
                "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k", quiet,
            ],
            check=True,
            capture_output=True,
        )
        loose = detect_silences(quiet, noise_db=-30.0, min_silence=0.25)
        self.assertEqual(len(loose), 1)
        self.assertLess(abs((loose[0][0] + loose[0][1]) / 2 - 1.6), 0.3)
        self.assertEqual(detect_silences(quiet, noise_db=-45.0, min_silence=0.25), [])

    def test_split_lossless(self):
        out_dir = os.path.join(self.tmp.name, "out_lossless")
        segments = build_segments(66.0, [13.5], 20.0)
        outputs = split_file(self.sample, segments, out_dir, prefix="p", reencode=False)
        self.assertEqual(len(outputs), len(segments))
        for path, segment in zip(outputs, segments):
            self.assertTrue(os.path.isfile(path))
            self.assertAlmostEqual(probe(path).duration, segment.duration, delta=0.3)

    def test_split_reencode(self):
        out_dir = os.path.join(self.tmp.name, "out_reencode")
        segments = build_segments(66.0, None, 33.0)
        outputs = split_file(self.sample, segments, out_dir, prefix="q", reencode=True)
        self.assertEqual(len(outputs), 2)
        for path, segment in zip(outputs, segments):
            self.assertTrue(os.path.isfile(path))
            self.assertAlmostEqual(probe(path).duration, segment.duration, delta=0.3)

    def test_split_with_redundancy(self):
        """带冗余的切割:输出时长与规划一致(相邻段重叠)。"""
        out_dir = os.path.join(self.tmp.name, "out_redundant")
        segments = build_segments(66.0, None, 20.0, merge_tail=True, redundancy=1.0)
        self.assertEqual(len(segments), 3)
        outputs = split_file(self.sample, segments, out_dir, prefix="r", reencode=False)
        self.assertEqual(len(outputs), len(segments))
        for path, segment in zip(outputs, segments):
            self.assertAlmostEqual(probe(path).duration, segment.duration, delta=0.3)

    def test_split_from_wav_input(self):
        """非 MP3 输入:重编码后应输出可读的 MP3 文件。"""
        wav = os.path.join(self.tmp.name, "tone.wav")
        subprocess.run(
            [ffmpeg_path(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=10", wav],
            check=True,
            capture_output=True,
        )
        out_dir = os.path.join(self.tmp.name, "out_wav")
        outputs = split_file(wav, build_segments(10.0, None, 5.0), out_dir, prefix="w", reencode=True)
        self.assertEqual(len(outputs), 2)
        for path in outputs:
            self.assertTrue(path.endswith(".mp3"))
            self.assertAlmostEqual(probe(path).duration, 5.0, delta=0.3)

    def test_separate_vocals_and_instrumental(self):
        """轻量引擎:立体声样本应生成人声与伴奏两个文件。

        测试样本左右声道内容相同(L=R),故伴奏(L-R)接近静音,
        而人声(中央声道)保留原内容——可据此验证算法方向正确。
        """
        out_dir = os.path.join(self.tmp.name, "out_sep")
        vocals, inst = separate_vocals(
            self.sample, out_dir, prefix="s", engine="light"
        )
        self.assertTrue(vocals.endswith("_vocals.mp3"))
        self.assertTrue(inst.endswith("_instrumental.mp3"))
        self.assertTrue(os.path.isfile(vocals))
        self.assertTrue(os.path.isfile(inst))
        self.assertAlmostEqual(probe(vocals).duration, 66.0, delta=0.6)
        self.assertAlmostEqual(probe(inst).duration, 66.0, delta=0.6)
        v_peaks = extract_waveform(vocals, points=300)
        i_peaks = extract_waveform(inst, points=300)
        self.assertGreater(max(v_peaks), 0.05)
        self.assertLess(max(i_peaks), 0.08)

    def test_separate_rejects_mono(self):
        """轻量引擎:单声道文件应拒绝分离并给出明确错误。"""
        mono = os.path.join(self.tmp.name, "mono.wav")
        subprocess.run(
            [ffmpeg_path(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", mono],
            check=True,
            capture_output=True,
        )
        out_dir = os.path.join(self.tmp.name, "out_mono")
        with self.assertRaises(AudioEngineError):
            separate_vocals(mono, out_dir, engine="light")

    @unittest.skipUnless(demucs_available(), "未安装 demucs,跳过 AI 分离测试")
    def test_separate_ai_engine(self):
        """AI 引擎(Demucs):应输出人声与伴奏两个 MP3,时长与源一致。

        CPU 推理较慢,用 10 秒短样本控制测试时长。
        """
        mini = os.path.join(self.tmp.name, "mini_ai.mp3")
        make_sample(mini, 10.0, parts=2)
        out_dir = os.path.join(self.tmp.name, "out_ai")
        vocals, inst = separate_vocals(mini, out_dir, prefix="ai", engine="ai")
        self.assertTrue(vocals.endswith("_vocals.mp3"))
        self.assertTrue(inst.endswith("_instrumental.mp3"))
        for path in (vocals, inst):
            self.assertTrue(os.path.isfile(path))
            self.assertAlmostEqual(probe(path).duration, 10.0, delta=0.6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
