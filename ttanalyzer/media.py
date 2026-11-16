"""ffmpeg helpers: probing, audio extraction, cutting and concatenating clips.

Uses the static ffmpeg binary shipped with the `imageio-ffmpeg` pip package,
so nothing needs to be installed system-wide.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

ProgressFn = Callable[[float, str], None]  # (fraction 0..1, message)


@dataclass
class VideoInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: str) -> VideoInfo:
    """Read duration / size / fps / audio presence from `ffmpeg -i` output."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, text=True, errors="replace")
    out = proc.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        raise ValueError(f"Could not read video: {os.path.basename(path)}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    width = height = 0
    fps = 30.0
    for line in out.splitlines():
        if "Video:" in line:
            sm = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            if sm:
                width, height = int(sm.group(1)), int(sm.group(2))
            fm = re.search(r"([\d.]+)\s*fps", line)
            if fm:
                fps = float(fm.group(1))
            # Phone videos often carry a rotation flag; ffmpeg auto-rotates on
            # decode, so swap the reported size to match what we'll get.
            break
    rot = re.search(r"rotat(?:e|ion)[^\d-]*(-?\d+)", out)
    if rot and abs(int(rot.group(1))) % 180 == 90:
        width, height = height, width
    has_audio = re.search(r"Stream #.*Audio:", out) is not None
    if not width:
        raise ValueError(f"No video stream in {os.path.basename(path)}")
    return VideoInfo(path, duration, width, height, fps, has_audio)


def extract_audio(path: str, rate: int = 16000) -> np.ndarray:
    """Return mono float32 audio samples in [-1, 1] (empty if no audio)."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
         "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _even(n: int) -> int:
    return n - (n % 2)


def output_size(infos: Iterable[VideoInfo], max_height: int = 1080) -> tuple[int, int]:
    """Common output size: the first video's size, capped at max_height."""
    first = next(iter(infos))
    w, h = first.width, first.height
    if h > max_height:
        w, h = int(w * max_height / h), max_height
    return _even(w), _even(h)


def _run_ffmpeg(args: list[str], duration: float, progress: ProgressFn | None,
                base: float, span: float, label: str) -> None:
    """Run ffmpeg, parsing `-progress` output to report fractional progress."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_us=") and progress and duration > 0:
            try:
                t = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            progress(base + span * min(1.0, t / duration), label)
    err = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed: {err.strip()[-800:]}")


def render_clip(info: VideoInfo, start: float, end: float, out_path: str,
                size: tuple[int, int], progress: ProgressFn | None = None,
                base: float = 0.0, span: float = 1.0, label: str = "") -> None:
    """Cut [start, end) from a video, re-encoded to a uniform format so that
    clips from different cameras/phones can be joined losslessly afterwards."""
    w, h = size
    start = max(0.0, start)
    end = min(info.duration, end)
    dur = max(0.05, end - start)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p")
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", info.path]
    if info.has_audio:
        amap = ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}",
                 "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        amap = ["-map", "0:v:0", "-map", "1:a:0"]
    args += [*amap, "-vf", vf,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
             "-video_track_timescale", "30000", "-movflags", "+faststart",
             out_path]
    _run_ffmpeg(args, dur, progress, base, span, label)


def concat_files(parts: list[str], out_path: str) -> None:
    """Join uniformly-encoded clips without re-encoding."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
        list_path = f.name
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", "-movflags", "+faststart", out_path],
            capture_output=True, text=True, errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {proc.stderr.strip()[-800:]}")
    finally:
        os.unlink(list_path)


@dataclass
class Segment:
    info: VideoInfo
    start: float
    end: float


def export_segments(segments: list[Segment], out_path: str, size: tuple[int, int],
                    progress: ProgressFn | None = None, title: str = "Exporting",
                    unit: str = "clip") -> str:
    """Render each segment to a temp clip, then join them into out_path."""
    if not segments:
        raise ValueError("Nothing to export — no clips match.")
    total = sum(max(0.05, s.end - s.start) for s in segments)
    done = 0.0
    with tempfile.TemporaryDirectory(prefix="ttclips_") as tmp:
        parts = []
        for i, seg in enumerate(segments):
            part = os.path.join(tmp, f"part_{i:04d}.mp4")
            dur = max(0.05, seg.end - seg.start)
            render_clip(seg.info, seg.start, seg.end, part, size, progress,
                        base=0.97 * done / total, span=0.97 * dur / total,
                        label=f"{title}: {unit} {i + 1} of {len(segments)}")
            parts.append(part)
            done += dur
        if progress:
            progress(0.98, f"{title}: joining {len(parts)} {unit}s into one")
        concat_files(parts, out_path)
    if progress:
        progress(1.0, f"{title}: done")
    return out_path
