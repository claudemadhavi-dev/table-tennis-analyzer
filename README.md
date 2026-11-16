# Table Tennis Analyzer

A Mac desktop app that takes up to 10 match videos and creates:

1. **`1_merged.mp4`** – all videos joined into one, in the order listed.
2. **`2_no_idle_time.mp4`** – only the rallies; time spent picking up balls, towelling, etc. is cut.
3. **`3_good_shots_points_won.mp4`** – only the rallies **you won**.
4. **`4_bad_shots_points_lost.mp4`** – only the rallies **you lost**.

Videos are saved to `~/Movies/Table Tennis Analyzer` (changeable in the app).

## Run it

Double-click **`Table Tennis Analyzer.command`**.
The first run installs the app's
Python packages into a local `.venv` folder; later runs start straight away.

## How to use

1. **Add videos…** – up to 10. Use ▲ ▼ to put them in match order.
2. **Choose which player is you…** – click on yourself in a frame from each
   video (players swap ends between games, so check each one). Pick *side-on*
   or *end-on* to match where the camera was.
3. Click any of the four buttons. Buttons ②–④ first find the rallies:
   - Rallies are found from the **sound of the ball** (bat hits and table bounces).
     If a video has no usable sound, the app uses on-screen movement instead.
     Use the *strict ↔ loose* slider if rallies are being missed or idle time is
     being kept, then click **Find rallies & guess winners** again.
   - The **winner of each rally is a guess**, based on which side of the table the
     ball finished on. Before exporting ③ or ④, click through the rally list on the
     right: each rally plays automatically; press **W** (won), **L** (lost) or
     **S** (skip) and it moves to the next one.
4. Your review is auto-saved as `review.ttproj` in the output folder. Reopen it
   with **File ▸ Open review…**.

## Files

- `ttanalyzer/media.py` – ffmpeg helpers (bundled ffmpeg from `imageio-ffmpeg`).
- `ttanalyzer/analysis.py` – rally detection and winner guessing.
- `ttanalyzer/gui.py` – the PySide6 window.
