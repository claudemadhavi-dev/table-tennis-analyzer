# Table Tennis Analyzer

Mac desktop app (Python + PySide6) that takes up to 10 table tennis match
videos and produces four outputs. User-facing docs are in `README.md`.

## Run

- Users launch by double-clicking `Table Tennis Analyzer.command` (creates
  `.venv` on first run, then starts `app.py`).
- Dev: `.venv/bin/python app.py`
- Python is the python.org install at `/usr/local/bin/python3` (3.15).
  There are no Xcode Command Line Tools, Homebrew or system ffmpeg on this Mac —
  don't depend on them.
- ffmpeg comes from the `imageio-ffmpeg` pip package (`media.FFMPEG`); always use
  that binary, never a system `ffmpeg`.

## Layout

- `ttanalyzer/media.py` — ffmpeg wrappers: `probe`, `extract_audio`,
  `render_clip` (re-encodes each piece to one uniform format: H.264/AAC, 30 fps,
  first video's size capped at 1080p, silent audio added if missing),
  `concat_files` (stream copy), `export_segments` (render + join).
- `ttanalyzer/analysis.py` — rally detection and winner guessing:
  - Rallies come from ball-hit *sounds* (sharp clicks, grouped when ≤2.2 s
    apart); falls back to frame motion when audio is missing/useless.
  - Winner guess = side of the table where the ball was last seen
    (ball on your side ⇒ you lost). It's rough; the user corrects it in the UI.
- `ttanalyzer/gui.py` — `MainWindow`, `PlayerPicker` (click on yourself in a
  frame; shift-click moves the net line), background `Worker` thread.

## Behaviour rules (from the user — keep these)

1. **Merge** joins whole videos end to end only. No rally detection, no player
   picking, no good/bad sorting, and the review panel stays hidden.
2. **Remove idle time** only needs rallies. It must **not** ask which player /
   side the user is, and must not show Won/Lost controls.
3. **Good shots** = rallies the user won; **bad shots** = rallies the user lost.
   Only these two ask the user to pick their player (per video, since players
   swap ends), and they reuse rallies already found by idle removal.
4. Winners are guesses: before the first good/bad export, offer a review where
   each rally plays and W / L / S sets won / lost / skip.

State flag: `MainWindow.winners_guessed` separates "rallies found" from
"winners worked out". The review is auto-saved as `review.ttproj` in the output
folder (default `~/Movies/Table Tennis Analyzer`).

## Testing

No test suite. To check the pipeline, generate synthetic videos (drawn table,
a white dot ball, click sounds at known rally times) with OpenCV + numpy, then
call `analysis.detect_rallies` / `guess_winners` / `media.export_segments`
directly and check output durations with `media.probe`. UI logic can be smoke-
tested headlessly with `QT_QPA_PLATFORM=offscreen` (call `app.processEvents()`
before checking widget visibility).

Known gaps: motion fallback is untested on real footage; iPhone HDR (Dolby
Vision HEVC) input isn't tone-mapped, so outputs may look washed out (user
deferred this).
