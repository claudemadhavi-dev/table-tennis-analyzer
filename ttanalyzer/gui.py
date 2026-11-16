"""Desktop UI (PySide6)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QPoint
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import analysis, media
from .analysis import DetectSettings, Rally

MAX_VIDEOS = 10
VIDEO_EXT = "Videos (*.mp4 *.mov *.m4v *.avi *.mkv *.MP4 *.MOV *.M4V *.AVI *.MKV)"
RESULT_COLORS = {"won": "#1f9d55", "lost": "#d64545", "skip": "#888888", "": "#b7791f"}
RESULT_TEXT = {"won": "Won  ✓", "lost": "Lost  ✗", "skip": "Skip", "": "?  (set it)"}


def fmt_time(t: float) -> str:
    t = max(0, int(round(t)))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ------------------------------------------------------------- worker ----

class Worker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            result = self.fn(lambda f, msg: self.progress.emit(f, msg))
            self.finished_ok.emit(result)
        except Exception as e:  # noqa: BLE001 - surface any failure to the user
            traceback.print_exc()
            self.failed.emit(str(e))


# ------------------------------------------------------- player picker ----

class FrameView(QLabel):
    """Shows a frame; click = 'this is me', shift-click = 'net is here'."""
    changed = Signal()

    def __init__(self, rgb, view: str, side: str, net: float):
        super().__init__()
        h, w, _ = rgb.shape
        img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.base = QPixmap.fromImage(img).scaled(640, 360, Qt.KeepAspectRatio,
                                                  Qt.SmoothTransformation)
        self.view, self.side, self.net = view, side, net
        self.click: QPoint | None = None
        self.setFixedSize(self.base.size())
        self.setCursor(Qt.CrossCursor)
        self.redraw()

    def mousePressEvent(self, ev):
        p = ev.position()
        x, y = p.x() / self.width(), p.y() / self.height()
        if ev.modifiers() & Qt.ShiftModifier:
            self.net = y if self.view == "end" else x
        else:
            self.click = ev.position().toPoint()
            self.side = analysis.side_of(x, y, self.view, self.net)
        self.redraw()
        self.changed.emit()

    def redraw(self):
        pm = QPixmap(self.base)
        p = QPainter(pm)
        W, H = pm.width(), pm.height()
        # shade "my" half
        if self.side:
            p.setBrush(QColor(31, 157, 85, 70))
            p.setPen(Qt.NoPen)
            if self.view == "end":
                top = self.side == "far"
                p.drawRect(0, 0 if top else int(self.net * H), W,
                           int(self.net * H) if top else H - int(self.net * H))
            else:
                left = self.side == "left"
                p.drawRect(0 if left else int(self.net * W), 0,
                           int(self.net * W) if left else W - int(self.net * W), H)
        p.setPen(QPen(QColor("#ffd400"), 2, Qt.DashLine))
        if self.view == "end":
            p.drawLine(0, int(self.net * H), W, int(self.net * H))
        else:
            p.drawLine(int(self.net * W), 0, int(self.net * W), H)
        if self.click:
            p.setPen(QPen(QColor("#1f9d55"), 3))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(self.click, 18, 18)
        p.end()
        self.setPixmap(pm)

    def set_view(self, view: str):
        self.view = view
        self.side, self.click = "", None
        self.redraw()


class PlayerPicker(QDialog):
    def __init__(self, parent, infos, view, sides, nets):
        super().__init__(parent)
        self.setWindowTitle("Which player are you?")
        self.infos, self.view = infos, view
        self.sides, self.nets = list(sides), list(nets)
        self.idx = 0

        lay = QVBoxLayout(self)
        intro = QLabel(
            "<b>Click on yourself</b> in each video. The app counts a point as "
            "<b>won</b> or <b>lost</b> from your side of the table.<br>"
            "Players swap ends between games, so check every video.  "
            "<i>Shift-click</i> to move the yellow net line if the table isn't centred.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Camera view:"))
        self.view_combo = QComboBox()
        self.view_combo.addItem("Side-on (players on left and right)", "side")
        self.view_combo.addItem("End-on (players near and far)", "end")
        self.view_combo.setCurrentIndex(0 if view == "side" else 1)
        self.view_combo.currentIndexChanged.connect(self._view_changed)
        row.addWidget(self.view_combo)
        row.addStretch()
        lay.addLayout(row)

        self.title = QLabel()
        lay.addWidget(self.title)
        self.holder = QVBoxLayout()
        lay.addLayout(self.holder)
        self.frame_view: FrameView | None = None

        nav = QHBoxLayout()
        self.prev_btn = QPushButton("◀ Previous video")
        self.next_btn = QPushButton("Next video ▶")
        self.prev_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn.clicked.connect(lambda: self._go(1))
        nav.addWidget(self.prev_btn)
        nav.addStretch()
        nav.addWidget(self.next_btn)
        lay.addLayout(nav)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._load()

    def _view_changed(self):
        self.view = self.view_combo.currentData()
        self.sides = [""] * len(self.sides)
        if self.frame_view:
            self.frame_view.set_view(self.view)
        self._update_title()

    def _store(self):
        if self.frame_view:
            self.sides[self.idx] = self.frame_view.side
            self.nets[self.idx] = self.frame_view.net

    def _go(self, d):
        self._store()
        self.idx = max(0, min(len(self.infos) - 1, self.idx + d))
        self._load()

    def _load(self):
        if self.frame_view:
            self.frame_view.setParent(None)
        info = self.infos[self.idx]
        rgb = analysis.grab_frame(info.path, min(5.0, info.duration / 2))
        if rgb is None:
            self.frame_view = None
            self.holder.addWidget(QLabel("Could not read a frame from this video."))
        else:
            self.frame_view = FrameView(rgb, self.view, self.sides[self.idx], self.nets[self.idx])
            self.frame_view.changed.connect(self._changed)
            self.holder.addWidget(self.frame_view)
        self.prev_btn.setEnabled(self.idx > 0)
        self.next_btn.setEnabled(self.idx < len(self.infos) - 1)
        self._update_title()

    def _changed(self):
        self._store()
        self._update_title()

    def _update_title(self):
        side = self.sides[self.idx] or "not chosen yet"
        self.title.setText(f"<b>Video {self.idx + 1} of {len(self.infos)}:</b> "
                           f"{os.path.basename(self.infos[self.idx].path)} — your side: <b>{side}</b>")

    def _accept(self):
        self._store()
        missing = [i + 1 for i, s in enumerate(self.sides) if not s]
        if missing:
            QMessageBox.warning(self, "Pick your player",
                                f"Click on yourself in video(s): {', '.join(map(str, missing))}")
            self.idx = missing[0] - 1
            self._load()
            return
        self.accept()


# --------------------------------------------------------- main window ----

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Table Tennis Analyzer")
        self.resize(1280, 820)

        self.infos: list[media.VideoInfo] = []
        self.sides: list[str] = []
        self.nets: list[float] = []
        self.view = "side"
        self.rallies: list[Rally] = []
        self.winners_guessed = False   # True once won/lost has been worked out
        self.worker: Worker | None = None
        self.out_dir = os.path.expanduser("~/Movies/Table Tennis Analyzer")

        self._build_menu()
        self._build_ui()
        self._set_review_visible(False)
        self._refresh()

    def _set_review_visible(self, on: bool):
        """The rally list only appears once rallies have been found (buttons
        ②–④); merging never shows it.  The won/lost controls only appear once
        winners have been worked out (buttons ③ and ④) — removing idle time
        doesn't need to know who you are."""
        for w in (self.video_widget, self.btn_play, self.rally_table):
            w.setVisible(on)
        scored = on and self.winners_guessed
        for w in (self.btn_won, self.btn_lost, self.btn_skip):
            w.setVisible(scored)
        self.rally_table.setColumnHidden(4, not scored)
        self.rally_table.setColumnHidden(5, not scored)

    # ---- layout ----
    def _build_menu(self):
        m = self.menuBar().addMenu("File")
        a = QAction("Open review…", self)
        a.setShortcut(QKeySequence.Open)
        a.triggered.connect(self.load_project)
        m.addAction(a)
        a = QAction("Save review…", self)
        a.setShortcut(QKeySequence.Save)
        a.triggered.connect(self.save_project_as)
        m.addAction(a)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split, 1)

        # ---- left column: inputs and the four actions
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        vids = QGroupBox(f"1 · Your videos (up to {MAX_VIDEOS}, in match order)")
        vl = QVBoxLayout(vids)
        self.video_table = QTableWidget(0, 3)
        self.video_table.setHorizontalHeaderLabels(["Video", "Length", "Your side"])
        self.video_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.video_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.video_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.video_table.verticalHeader().setVisible(False)
        vl.addWidget(self.video_table)
        row = QHBoxLayout()
        for text, fn in [("Add videos…", self.add_videos), ("Remove", self.remove_video),
                         ("▲", lambda: self.move_video(-1)), ("▼", lambda: self.move_video(1))]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        vl.addLayout(row)
        self.pick_btn = QPushButton("Choose which player is you…")
        self.pick_btn.clicked.connect(self.pick_player)
        vl.addWidget(self.pick_btn)
        ll.addWidget(vids)

        opts = QGroupBox("2 · Settings")
        og = QGridLayout(opts)
        og.addWidget(QLabel("Save videos to:"), 0, 0)
        self.out_edit = QLineEdit(self.out_dir)
        og.addWidget(self.out_edit, 0, 1)
        b = QPushButton("…")
        b.clicked.connect(self.choose_out_dir)
        og.addWidget(b, 0, 2)
        og.addWidget(QLabel("Rally detection:"), 1, 0)
        self.sens = QSlider(Qt.Horizontal)
        self.sens.setRange(0, 100)
        self.sens.setValue(50)
        og.addWidget(self.sens, 1, 1)
        og.addWidget(QLabel("strict ↔ loose"), 1, 2)
        ll.addWidget(opts)

        acts = QGroupBox("3 · Create videos")
        al = QVBoxLayout(acts)
        self.btn_merge = QPushButton("①  Merge all videos into one")
        self.btn_idle = QPushButton("②  Remove idle time (rallies only)")
        self.btn_good = QPushButton("③  Good shots — points you won")
        self.btn_bad = QPushButton("④  Bad shots — points you lost")
        self.btn_find = QPushButton("Find rallies again")
        self.btn_merge.clicked.connect(self.do_merge)
        self.btn_idle.clicked.connect(lambda: self.do_export("idle"))
        self.btn_good.clicked.connect(lambda: self.do_export("won"))
        self.btn_bad.clicked.connect(lambda: self.do_export("lost"))
        self.btn_find.clicked.connect(
            lambda: self.run_analysis(need_winners=self.winners_guessed))
        for b in (self.btn_merge, self.btn_idle, self.btn_good, self.btn_bad):
            b.setMinimumHeight(38)
            al.addWidget(b)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        al.addWidget(line)
        al.addWidget(self.btn_find)
        ll.addWidget(acts)
        ll.addStretch()
        split.addWidget(left)

        # ---- right column: rally review
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel("Add videos, then click a button on the left.")
        self.summary.setWordWrap(True)
        rl.addWidget(self.summary)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(300)
        self.player.setVideoOutput(self.video_widget)
        self.player.positionChanged.connect(self._on_position)
        self.play_end_ms = None
        self.current_source = ""
        rl.addWidget(self.video_widget, 3)

        rb = QHBoxLayout()
        self.btn_play = QPushButton("▶ Replay rally  (Space)")
        self.btn_won = QPushButton("Won  (W)")
        self.btn_lost = QPushButton("Lost  (L)")
        self.btn_skip = QPushButton("Skip  (S)")
        self.btn_play.clicked.connect(self.play_selected)
        self.btn_won.clicked.connect(lambda: self.set_result("won"))
        self.btn_lost.clicked.connect(lambda: self.set_result("lost"))
        self.btn_skip.clicked.connect(lambda: self.set_result("skip"))
        self.btn_won.setStyleSheet(f"color: {RESULT_COLORS['won']}; font-weight: bold;")
        self.btn_lost.setStyleSheet(f"color: {RESULT_COLORS['lost']}; font-weight: bold;")
        for b in (self.btn_play, self.btn_won, self.btn_lost, self.btn_skip):
            rb.addWidget(b)
        rl.addLayout(rb)

        self.rally_table = QTableWidget(0, 6)
        self.rally_table.setHorizontalHeaderLabels(
            ["#", "Video", "Starts at", "Length", "Ball ended", "Result"])
        self.rally_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.rally_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rally_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.rally_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rally_table.verticalHeader().setVisible(False)
        self.rally_table.itemSelectionChanged.connect(self.play_selected)
        rl.addWidget(self.rally_table, 2)
        split.addWidget(right)
        split.setSizes([430, 850])

        bottom = QHBoxLayout()
        self.status = QLabel("Ready")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setMaximumWidth(360)
        self.reveal_btn = QPushButton("Show in Finder")
        self.reveal_btn.clicked.connect(self.reveal)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.bar)
        bottom.addWidget(self.reveal_btn)
        outer.addLayout(bottom)

        for key, fn in [("W", lambda: self.set_result("won")),
                        ("L", lambda: self.set_result("lost")),
                        ("S", lambda: self.set_result("skip")),
                        ("Space", self.play_selected)]:
            QShortcut(QKeySequence(key), self, activated=fn)

    # ---- videos ----
    def add_videos(self):
        room = MAX_VIDEOS - len(self.infos)
        if room <= 0:
            QMessageBox.information(self, "Limit reached", f"You can add up to {MAX_VIDEOS} videos.")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add table tennis videos", "", VIDEO_EXT)
        if not paths:
            return
        if len(paths) > room:
            QMessageBox.information(self, "Limit reached",
                                    f"Only the first {room} were added (max {MAX_VIDEOS} videos).")
            paths = paths[:room]
        errors = []
        for p in sorted(paths):
            try:
                self.infos.append(media.probe(p))
                self.sides.append("")
                self.nets.append(0.5)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
        if errors:
            QMessageBox.warning(self, "Some videos were skipped", "\n".join(errors))
        self._videos_changed()

    def remove_video(self):
        r = self.video_table.currentRow()
        if r < 0:
            return
        for lst in (self.infos, self.sides, self.nets):
            del lst[r]
        self._videos_changed()

    def move_video(self, d):
        r = self.video_table.currentRow()
        j = r + d
        if r < 0 or not (0 <= j < len(self.infos)):
            return
        for lst in (self.infos, self.sides, self.nets):
            lst[r], lst[j] = lst[j], lst[r]
        self._videos_changed()
        self.video_table.selectRow(j)

    def _videos_changed(self):
        # rally indices point into the video list, so any change invalidates them
        if self.rallies:
            self.rallies = []
            self.winners_guessed = False
            self._fill_rallies()
        self._refresh()

    def pick_player(self) -> bool:
        if not self.infos:
            QMessageBox.information(self, "No videos", "Add your videos first.")
            return False
        dlg = PlayerPicker(self, self.infos, self.view, self.sides, self.nets)
        if dlg.exec() != QDialog.Accepted:
            return False
        view_changed = dlg.view != self.view
        sides_changed = dlg.sides != self.sides or dlg.nets != self.nets
        self.view, self.sides, self.nets = dlg.view, dlg.sides, dlg.nets
        self._refresh()
        if self.rallies and (view_changed or sides_changed):
            # re-derive guesses but keep anything the player already set by hand
            for r in self.rallies:
                if r.result == r.guess:
                    r.result = ""
            self.run_analysis(only_guess=True)
        return True

    def choose_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Save videos to", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _refresh(self):
        t = self.video_table
        t.setRowCount(len(self.infos))
        for i, info in enumerate(self.infos):
            t.setItem(i, 0, QTableWidgetItem(os.path.basename(info.path)))
            t.setItem(i, 1, QTableWidgetItem(fmt_time(info.duration)))
            side = QTableWidgetItem(self.sides[i] or "— pick —")
            if not self.sides[i]:
                side.setForeground(QColor(RESULT_COLORS[""]))
            t.setItem(i, 2, side)
        has = bool(self.infos)
        busy = self.worker is not None
        for b in (self.btn_merge, self.btn_idle, self.btn_good, self.btn_bad,
                  self.btn_find, self.pick_btn):
            b.setEnabled(has and not busy)
        self._update_summary()

    # ---- rallies ----
    def run_analysis(self, then: Callable | None = None, only_guess: bool = False,
                     need_winners: bool = True):
        """Find rallies (from ball sounds / motion).  Only when need_winners is
        set does it ask which player you are and guess who won each rally."""
        if not self.infos:
            return
        if need_winners and not all(self.sides) and not self.pick_player():
            return
        infos = list(self.infos)
        settings = DetectSettings(sensitivity=self.sens.value() / 100)
        sides, view, nets = list(self.sides), self.view, list(self.nets)
        existing = self.rallies if only_guess else None

        def job(progress):
            rallies = existing if existing is not None else analysis.detect_rallies(
                infos, settings, lambda f, m: progress(0.6 * f, m))
            if need_winners:
                analysis.guess_winners(infos, rallies, sides, view, nets,
                                       lambda f, m: progress(0.6 + 0.4 * f, m))
            return rallies

        def done(rallies):
            self.rallies = rallies
            if need_winners:
                self.winners_guessed = True
            elif not only_guess:
                self.winners_guessed = False
            self._fill_rallies()
            self._autosave()
            if then:
                then()

        self._start(job, done)

    def _fill_rallies(self):
        t = self.rally_table
        t.blockSignals(True)
        t.setRowCount(len(self.rallies))
        for i, r in enumerate(self.rallies):
            vals = [str(i + 1), os.path.basename(self.infos[r.video].path),
                    fmt_time(r.start), f"{r.duration:.1f}s", r.ball_end or "not seen"]
            for c, v in enumerate(vals):
                t.setItem(i, c, QTableWidgetItem(v))
            self._paint_result(i)
        t.blockSignals(False)
        self._set_review_visible(bool(self.rallies))
        self._update_summary()

    def _paint_result(self, i):
        r = self.rallies[i]
        item = QTableWidgetItem(RESULT_TEXT[r.result])
        item.setForeground(QColor(RESULT_COLORS[r.result]))
        f = item.font()
        f.setBold(True)
        item.setFont(f)
        self.rally_table.setItem(i, 5, item)

    def _update_summary(self):
        if not self.infos:
            self.summary.setText("Add your videos (up to 10), then choose a button on the left.")
            return
        total = sum(i.duration for i in self.infos)
        if not self.rallies:
            self.summary.setText(
                f"{len(self.infos)} video(s), {fmt_time(total)} in total.<br><br>"
                "<b>①</b> joins the whole videos end to end — nothing is cut or sorted.<br>"
                "<b>②–④</b> find the rallies; you can then check who won each one here.")
            return
        play = sum(r.duration for r in self.rallies)
        won = sum(r.result == "won" for r in self.rallies)
        lost = sum(r.result == "lost" for r in self.rallies)
        unk = sum(r.result == "" for r in self.rallies)
        if not self.winners_guessed:
            self.summary.setText(
                f"<b>{len(self.rallies)} rallies</b> · playing time {fmt_time(play)} of "
                f"{fmt_time(total)} ({100 * play / max(total, 1):.0f}%)<br><small>Click a "
                "rally to watch it. Use the strict ↔ loose slider and <b>Find rallies</b> "
                "again if rallies are missed or idle time is kept.</small>")
            return
        txt = (f"<b>{len(self.rallies)} rallies</b> · playing time {fmt_time(play)} of "
               f"{fmt_time(total)} ({100 * play / max(total, 1):.0f}%) · "
               f"<span style='color:{RESULT_COLORS['won']}'><b>{won} won</b></span> · "
               f"<span style='color:{RESULT_COLORS['lost']}'><b>{lost} lost</b></span>")
        if unk:
            txt += (f" · <span style='color:{RESULT_COLORS['']}'><b>{unk} need a result</b></span>")
        txt += ("<br><small>Winners are a best guess from where the ball ended up. "
                "Click a rally to watch it, then press <b>W</b> / <b>L</b> to correct it.</small>")
        self.summary.setText(txt)

    def selected_index(self) -> int:
        rows = self.rally_table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def play_selected(self):
        i = self.selected_index()
        if i < 0:
            return
        r = self.rallies[i]
        path = self.infos[r.video].path
        if self.current_source != path:
            self.player.setSource(QUrl.fromLocalFile(path))
            self.current_source = path
        self.play_end_ms = int(r.end * 1000)
        self.player.setPosition(int(r.start * 1000))
        self.player.play()

    def _on_position(self, pos):
        if self.play_end_ms is not None and pos >= self.play_end_ms:
            self.player.pause()
            self.play_end_ms = None

    def set_result(self, value):
        i = self.selected_index()
        if i < 0 or not self.winners_guessed:
            return
        self.rallies[i].result = value
        self._paint_result(i)
        self._update_summary()
        self._autosave()
        if i + 1 < len(self.rallies):          # move on to the next rally
            self.rally_table.selectRow(i + 1)

    # ---- exports ----
    def _out_path(self, name) -> str:
        d = self.out_edit.text().strip() or self.out_dir
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    def do_merge(self):
        infos = list(self.infos)
        out = self._out_path("1_merged.mp4")
        size = media.output_size(infos)
        # Merge = whole videos joined end to end. No rally detection, no won/lost.
        segs = [media.Segment(i, 0.0, i.duration) for i in infos]
        self._start(lambda p: media.export_segments(
                        segs, out, size, p, "Merging (whole videos, nothing cut)",
                        unit="video"),
                    self._exported)

    def do_export(self, kind: str):
        if kind == "idle":
            if not self.rallies:   # idle removal: no player / side needed
                self.run_analysis(then=lambda: self.do_export(kind), need_winners=False)
                return
        elif not self.rallies:
            self.run_analysis(then=lambda: self.do_export(kind))
            return
        elif not self.winners_guessed:   # rallies already found by ②
            self.run_analysis(then=lambda: self.do_export(kind), only_guess=True)
            return
        if kind == "idle":
            chosen = self.rallies
            name, title = "2_no_idle_time.mp4", "Removing idle time"
        else:
            if not getattr(self, "review_offered", False):
                self.review_offered = True
                ans = QMessageBox.question(
                    self, "Check the results first?",
                    "Who won each rally was guessed from where the ball ended up, "
                    "so some will be wrong.\n\nReview the rallies now? Each one plays; "
                    "press W (won) or L (lost) to correct it.\n\n"
                    "Choose No to export the guesses as they are.")
                if ans == QMessageBox.Yes:
                    self.rally_table.setFocus()
                    self.rally_table.selectRow(0)
                    return
            unk = sum(r.result == "" for r in self.rallies)
            if unk:
                ans = QMessageBox.question(
                    self, "Some rallies have no result",
                    f"{unk} rallies have no won/lost result yet and will be left out.\n\n"
                    "Export anyway? (Choose No to review them first.)")
                if ans != QMessageBox.Yes:
                    return
            chosen = [r for r in self.rallies if r.result == kind]
            if kind == "won":
                name, title = "3_good_shots_points_won.mp4", "Good shots"
            else:
                name, title = "4_bad_shots_points_lost.mp4", "Bad shots"
        if not chosen:
            QMessageBox.information(self, "Nothing to export", "No rallies match.")
            return
        infos = list(self.infos)
        segs = [media.Segment(infos[r.video], r.start, r.end) for r in chosen]
        out = self._out_path(name)
        size = media.output_size(infos)
        self._start(lambda p: media.export_segments(segs, out, size, p, title),
                    self._exported)

    def _exported(self, path):
        self.last_output = path
        self.status.setText(f"Saved {os.path.basename(path)}")
        ans = QMessageBox.question(self, "Video ready",
                                   f"Saved:\n{path}\n\nOpen it now?")
        if ans == QMessageBox.Yes:
            subprocess.run(["open", path])

    def reveal(self):
        path = getattr(self, "last_output", None)
        if path and os.path.exists(path):
            subprocess.run(["open", "-R", path])
        else:
            d = self.out_edit.text().strip()
            os.makedirs(d, exist_ok=True)
            subprocess.run(["open", d])

    # ---- background jobs ----
    def _start(self, fn, on_done):
        if self.worker:
            return
        self.player.pause()
        self.worker = Worker(fn, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(lambda res: self._job_done(on_done, res))
        self.worker.failed.connect(self._job_failed)
        self.bar.setValue(0)
        self._refresh()
        self.worker.start()

    def _on_progress(self, frac, msg):
        self.bar.setValue(int(frac * 1000))
        self.status.setText(msg)

    def _job_done(self, on_done, result):
        self.worker = None
        self.bar.setValue(1000)
        self._refresh()
        on_done(result)

    def _job_failed(self, msg):
        self.worker = None
        self.bar.setValue(0)
        self.status.setText("Failed")
        self._refresh()
        QMessageBox.critical(self, "Something went wrong", msg)

    # ---- save / load review ----
    def _project_dict(self) -> dict:
        return {"videos": [i.path for i in self.infos], "sides": self.sides,
                "nets": self.nets, "view": self.view,
                "rallies": [r.to_dict() for r in self.rallies]}

    def _autosave(self):
        try:
            with open(self._out_path("review.ttproj"), "w") as f:
                json.dump(self._project_dict(), f, indent=1)
        except OSError:
            pass

    def save_project_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save review", self._out_path("review.ttproj"),
                                              "Review (*.ttproj)")
        if path:
            with open(path, "w") as f:
                json.dump(self._project_dict(), f, indent=1)

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open review", self.out_edit.text(),
                                              "Review (*.ttproj)")
        if not path:
            return
        try:
            with open(path) as f:
                d = json.load(f)
            self.infos = [media.probe(p) for p in d["videos"]]
            self.sides, self.nets, self.view = d["sides"], d["nets"], d["view"]
            self.rallies = [Rally.from_dict(r) for r in d["rallies"]]
            self.winners_guessed = any(r.guess or r.result for r in self.rallies)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not open review", str(e))
            return
        self._fill_rallies()
        self._refresh()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Table Tennis Analyzer")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
