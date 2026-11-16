"""Rally detection and point-winner guessing.

How it works
------------
* Rallies are found from the *sound* of the ball: every bat hit and table
  bounce is a sharp click.  Clicks closer than ~2 s apart are grouped into one
  rally; long quiet stretches (picking up the ball, towelling, walking) are
  idle time.  If a video has no usable audio, frame-to-frame motion is used.
* The winner of each rally is *guessed* by tracking the ball for the last
  second of the rally: the ball usually ends up on the side of the player who
  failed to return it.  This is a rough guess — the app always lets the
  player confirm or flip each result before exporting.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Callable, Optional

import cv2
import numpy as np

from .media import VideoInfo, extract_audio

ProgressFn = Callable[[float, str], None]

SIDES = ("left", "right", "near", "far")


@dataclass
class Rally:
    video: int            # index into the project's video list
    start: float          # seconds, padded (includes serve toss)
    end: float            # seconds, padded (includes reaction after point)
    hits: int = 0         # number of ball sounds detected
    ball_end: str = ""    # side where the ball was last seen ("left", ...)
    guess: str = ""       # "won" / "lost" / "" (unknown) — from the tracker
    result: str = ""      # "won" / "lost" / "skip" / "" — what gets exported

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rally":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class DetectSettings:
    sensitivity: float = 0.5   # 0 (strict) … 1 (catch everything)
    max_gap: float = 2.2       # max seconds between hits inside one rally
    min_hits: int = 3          # fewer sounds than this = not a rally
    pad_before: float = 1.2    # keep the serve toss
    pad_after: float = 1.5     # keep the reaction


# ---------------------------------------------------------------- audio ----

def _rolling_background(e: np.ndarray, block: int) -> np.ndarray:
    """Cheap rolling median: per-block medians, linearly interpolated."""
    n = len(e)
    if n == 0:
        return e
    centers, meds = [], []
    for i in range(0, n, block):
        chunk = e[i:i + block]
        centers.append(i + len(chunk) / 2)
        meds.append(np.median(chunk))
    return np.interp(np.arange(n), centers, meds)


def detect_hits(audio: np.ndarray, rate: int, sensitivity: float) -> np.ndarray:
    """Return times (s) of sharp click-like sounds (ball hits / bounces)."""
    if audio.size < rate:
        return np.zeros(0)
    hop = rate // 100                          # 10 ms frames
    hp = np.diff(audio)                        # crude high-pass: clicks are bright
    n = len(hp) // hop
    frames = hp[: n * hop].reshape(n, hop)
    e = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)

    bg = _rolling_background(e, 200)           # 2 s blocks
    ratio = 7.0 - 4.5 * sensitivity            # 7x (strict) … 2.5x (loose)
    floor = np.percentile(e, 60) * (1.8 - sensitivity)
    cand = (e > bg * ratio) & (e > floor)

    # local maxima only, then enforce a minimum spacing of 120 ms
    peaks = []
    last = -1e9
    for i in np.flatnonzero(cand):
        lo, hi = max(0, i - 4), min(n, i + 5)
        if e[i] < e[lo:hi].max():
            continue
        t = i * hop / rate
        if t - last >= 0.12:
            peaks.append(t)
            last = t
    return np.array(peaks)


def group_rallies(hits: np.ndarray, s: DetectSettings, duration: float,
                  video: int) -> list[Rally]:
    rallies: list[Rally] = []
    if hits.size == 0:
        return rallies
    group = [hits[0]]
    for t in hits[1:]:
        if t - group[-1] <= s.max_gap:
            group.append(t)
        else:
            _close_group(group, s, duration, video, rallies)
            group = [t]
    _close_group(group, s, duration, video, rallies)
    return _merge_overlaps(rallies)


def _close_group(group, s, duration, video, out):
    if len(group) < s.min_hits or group[-1] - group[0] < 0.6:
        return
    out.append(Rally(video=video,
                     start=max(0.0, group[0] - s.pad_before),
                     end=min(duration, group[-1] + s.pad_after),
                     hits=len(group)))


def _merge_overlaps(rallies: list[Rally]) -> list[Rally]:
    merged: list[Rally] = []
    for r in sorted(rallies, key=lambda r: r.start):
        if merged and r.start <= merged[-1].end:
            m = merged[-1]
            m.end = max(m.end, r.end)
            m.hits += r.hits
        else:
            merged.append(r)
    return merged


# --------------------------------------------------------------- motion ----

def motion_profile(path: str, sample_fps: float = 6.0,
                   progress: Optional[ProgressFn] = None, base=0.0, span=1.0
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Mean absolute frame difference sampled at ~sample_fps (times, values)."""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    step = max(1, int(round(fps / sample_fps)))
    times, vals = [], []
    prev = None
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            small = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
            if prev is not None:
                times.append(idx / fps)
                vals.append(float(cv2.absdiff(small, prev).mean()))
            prev = small
            if progress and idx % (step * 50) == 0:
                progress(base + span * min(1.0, idx / total), "Measuring motion")
        idx += 1
    cap.release()
    return np.array(times), np.array(vals)


def rallies_from_motion(times, vals, s: DetectSettings, duration, video) -> list[Rally]:
    if vals.size < 10:
        return []
    med = np.median(vals)
    mad = np.median(np.abs(vals - med)) + 1e-6
    thr = med + (2.5 - 2.0 * s.sensitivity) * mad
    active = times[vals > thr]
    # treat each active sample like a "hit" and reuse the grouping logic
    fake = DetectSettings(s.sensitivity, s.max_gap, max(4, s.min_hits),
                          s.pad_before, s.pad_after)
    return group_rallies(active, fake, duration, video)


# ---------------------------------------------------------- ball tracker ----

def _ball_positions(cap, t0: float, t1: float, width: int = 640):
    """Small, bright, moving blobs between t0 and t1 → list of (t, x, y) in
    normalised 0..1 frame coordinates."""
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t0) * 1000)
    prev = None
    pts = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t > t1:
            break
        h0, w0 = frame.shape[:2]
        scale = width / w0
        frame = cv2.resize(frame, (width, int(h0 * scale)))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev is not None:
            diff = cv2.absdiff(gray, prev)
            _, mask = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
            # ball colour: white (bright, low saturation) or orange
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            white = cv2.inRange(hsv, (0, 0, 170), (180, 70, 255))
            orange = cv2.inRange(hsv, (5, 120, 150), (25, 255, 255))
            mask = cv2.bitwise_and(mask, cv2.bitwise_or(white, orange))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
            n, _, stats, cents = cv2.connectedComponentsWithStats(mask)
            H, W = gray.shape
            best = None
            for i in range(1, n):
                x, y, w, h, area = stats[i]
                if not (3 <= area <= 120):
                    continue
                if not (0.4 <= w / max(h, 1) <= 2.5):
                    continue
                # ignore the very edges of the frame (scoreboards, people walking past)
                cx, cy = cents[i]
                if cx < 0.03 * W or cx > 0.97 * W or cy < 0.03 * H or cy > 0.97 * H:
                    continue
                score = area
                if best is None or score > best[0]:
                    best = (score, cx / W, cy / H)
            if best is not None:
                pts.append((t, best[1], best[2]))
        prev = gray
    return pts


def side_of(x: float, y: float, view: str, net: float) -> str:
    if view == "end":     # camera behind one player: players near/far
        return "far" if y < net else "near"
    return "left" if x < net else "right"


def guess_winners(infos: list[VideoInfo], rallies: list[Rally],
                  player_sides: list[str], view: str, net_pos: list[float],
                  progress: Optional[ProgressFn] = None) -> None:
    """Fill rally.ball_end / guess / result in place."""
    caps: dict[int, cv2.VideoCapture] = {}
    try:
        for i, r in enumerate(rallies):
            if progress:
                progress(i / max(1, len(rallies)), f"Guessing winners: rally {i + 1}/{len(rallies)}")
            cap = caps.get(r.video)
            if cap is None:
                cap = caps[r.video] = cv2.VideoCapture(infos[r.video].path)
            core_end = r.end - 1.5  # roughly the last ball sound
            pts = _ball_positions(cap, core_end - 1.0, core_end + 0.8)
            r.ball_end, r.guess = "", ""
            if len(pts) >= 3:
                last = pts[-4:]
                x = float(np.median([p[1] for p in last]))
                y = float(np.median([p[2] for p in last]))
                r.ball_end = side_of(x, y, view, net_pos[r.video])
                mine = player_sides[r.video]
                # ball finishing on your side usually means you missed it
                r.guess = "lost" if r.ball_end == mine else "won"
            if not r.result:
                r.result = r.guess
    finally:
        for c in caps.values():
            c.release()


# ---------------------------------------------------------------- driver ----

def detect_rallies(infos: list[VideoInfo], settings: DetectSettings,
                   progress: Optional[ProgressFn] = None) -> list[Rally]:
    all_rallies: list[Rally] = []
    n = len(infos)
    for vi, info in enumerate(infos):
        base, span = vi / n, 1.0 / n
        name = info.path.rsplit("/", 1)[-1]
        if progress:
            progress(base, f"Listening for ball hits in {name}")
        rallies: list[Rally] = []
        if info.has_audio:
            audio = extract_audio(info.path)
            hits = detect_hits(audio, 16000, settings.sensitivity)
            rallies = group_rallies(hits, settings, info.duration, vi)
        # too few rallies for the length of the video → audio is probably
        # useless (music, muted, far-away mic): fall back to motion.
        if len(rallies) < max(1, info.duration / 120):
            t, v = motion_profile(info.path, progress=progress,
                                  base=base + 0.3 * span, span=0.7 * span)
            motion = rallies_from_motion(t, v, settings, info.duration, vi)
            if len(motion) > len(rallies):
                rallies = motion
        all_rallies.extend(rallies)
    if progress:
        progress(1.0, f"Found {len(all_rallies)} rallies")
    return all_rallies


def grab_frame(path: str, t: float = 5.0):
    """A representative RGB frame (for the player picker)."""
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    if not ok:
        cap.set(cv2.CAP_PROP_POS_MSEC, 0)
        ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
