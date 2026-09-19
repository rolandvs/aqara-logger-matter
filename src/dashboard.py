#!/usr/bin/env python3
"""
dashboard.py - fullscreen display of the Aqara T1 sensors (reads latest.json from aqara_logger.py).

    python3 dashboard.py /opt/aqara/app/latest.json            # fullscreen, Esc = quit
    python3 dashboard.py latest.json --windowed 800x480         # test on a PC
    python3 dashboard.py --corrections                          # apply corrections.json
    python3 dashboard.py --corrections /path/to/corrections.json

Layout adapts to the screen: 800x480 landscape and 720x1280 portrait both work.

--corrections applies the same per-sensor corrections as aqara-export (made by
aqara-export calibrate --write). Corrected values get a * and the status bar says so.
The file is re-read when it changes, so a new calibration shows up without a restart.
Without the option the tiles show the raw values, exactly as logged.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

REFRESH_MS = 5_000
# A T1 checks in every ~55 min even without changes (measured: 54.8 min, never more
# than 55.0 in 373 gaps). One check-in overdue -> time shown orange ("late");
# two overdue -> value grey as well ("silent"). See aqara-health for the history.
HEARTBEAT_S = 55 * 60
TOL_S = 5 * 60
STALE_FILE_S = 900          # latest.json older than this -> logger stopped, warn loudly

BG, CARD, FG, DIM, STALE, WARN = "#0f1316", "#1b2126", "#f2f4f5", "#8b959d", "#555d63", "#e0a030"


EP_SUFFIX = re.compile(r"\((ep\d+)\)\s*$")


def short_name(name: str) -> str:
    """"Temperature and Humidity Sensor T1 (ep3)" -> "ep3"; other names stay as they are."""
    m = EP_SUFFIX.search(name)
    return m.group(1) if m else name


def sort_key(name: str) -> tuple[int, str, int]:
    m = EP_SUFFIX.search(name)
    return (0, "", int(m.group(1)[2:])) if m else (1, name, 0)


def local_hm(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M") if iso else "--:--"


def age_s(iso: str | None) -> float:
    if not iso:
        return math.inf
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


class Dashboard:
    def __init__(self, root: tk.Tk, path: Path, corrections: Path | None = None) -> None:
        self.root, self.path = root, path
        self.corr_path = corrections
        self.corr: dict[str, tuple[float, float]] = {}   # sensor id -> (add °C, add %RH)
        self.corr_error = ""
        self._corr_mtime: float | None = None
        self.names: list[str] | None = None
        self.built_size: tuple[int, int] | None = None
        self._shown: dict[str, dict] = {}              # label -> options last drawn
        self.cards: dict[str, dict[str, tk.Label]] = {}
        root.configure(bg=BG)
        self.status = tk.Label(root, bg=BG, fg=DIM, anchor="e", padx=10)
        self.status.pack(side="bottom", fill="x")
        self.grid = tk.Frame(root, bg=BG)
        self.grid.pack(side="top", fill="both", expand=True)
        root.after(300, self.refresh)   # let fullscreen geometry settle first

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def on_configure(self, event: tk.Event) -> None:
        """Rebuild the tiles only when the window really changed size.

        X11 also sends Configure when a label's new text changes its width, at the
        same window size; rebuilding on those made every refresh blink."""
        if event.widget is not self.root or self.built_size is None:
            return
        if (event.width, event.height) != self.built_size and event.width > 1:
            self.names = None

    def put(self, lbl: tk.Label, **opts) -> None:
        """Configure a label only with what actually changed: every configure makes Tk
        redraw the label (and a remote desktop resend it), even with identical text."""
        shown = self._shown.setdefault(str(lbl), {})
        changed = {k: v for k, v in opts.items() if shown.get(k) != v}
        if changed:
            lbl.configure(**changed)
            shown.update(changed)

    def load_corrections(self) -> None:
        """Same file and fields as aqara-export; re-read only when it changed."""
        if self.corr_path is None:
            return
        try:
            mtime = self.corr_path.stat().st_mtime
        except OSError:
            self.corr, self.corr_error, self._corr_mtime = {}, "corrections file not found", None
            return
        if mtime == self._corr_mtime:
            return
        self._corr_mtime = mtime
        try:
            text = self.corr_path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}     # empty = no corrections yet
        except (OSError, ValueError):
            self.corr, self.corr_error = {}, "corrections file unreadable"
            return
        if not isinstance(data, dict):
            self.corr, self.corr_error = {}, "corrections file unreadable"
            return
        num = lambda v: float(v) if isinstance(v, (int, float)) else 0.0
        self.corr = {k: (num(v.get("add_temp_c")), num(v.get("add_rh_pct")))
                     for k, v in data.items()
                     if not k.startswith("_") and isinstance(v, dict)}
        self.corr_error = ""

    def build(self, names: list[str]) -> None:
        for child in self.grid.winfo_children():
            child.destroy()
        self.cards.clear()
        self._shown.clear()
        self.names = names
        n = max(len(names), 1)
        w, h = self.root.winfo_width(), self.root.winfo_height()
        self.built_size = (w, h)
        self.grid.grid_propagate(False)                 # text changes must not resize anything
        cols = 1 if n == 1 else (2 if h > w or n <= 4 else 3)
        rows = math.ceil(n / cols)
        cell_w, cell_h = w / cols, (h - 30) / rows
        px = lambda f: -max(10, int(f))     # negative size = pixels in Tk
        # a long name (before it is renamed in names.json) must not push the card apart
        name_px = lambda text: px(min(cell_h * 0.11, cell_w * 1.5 / max(len(text), 8)))
        f_temp = ("DejaVu Sans", px(min(cell_h * 0.36, cell_w * 0.21)), "bold")
        f_sub = ("DejaVu Sans", px(cell_h * 0.10))
        self.status.configure(font=("DejaVu Sans", px(16)))

        for c in range(cols):
            self.grid.columnconfigure(c, weight=1, uniform="col")
        for r in range(rows):
            self.grid.rowconfigure(r, weight=1, uniform="row")
        for i, name in enumerate(names):
            card = tk.Frame(self.grid, bg=CARD)
            card.grid(row=i // cols, column=i % cols, sticky="nsew", padx=5, pady=5)
            card.pack_propagate(False)
            short = short_name(name)
            labels = {
                "name": tk.Label(card, text=short, bg=CARD, fg=DIM,
                                 font=("DejaVu Sans", name_px(short))),
                "temp": tk.Label(card, bg=CARD, fg=FG, font=f_temp),
                "hum":  tk.Label(card, bg=CARD, fg=FG, font=f_sub),
                "foot": tk.Label(card, bg=CARD, fg=DIM, font=f_sub),
            }
            for lbl in labels.values():
                lbl.pack(expand=True)
            self.cards[name] = labels

    def refresh(self) -> None:
        try:
            data = self.load()
            if not data or not data.get("sensors"):
                if self.names != []:
                    self.build([])
                self.put(self.status, text=f"Waiting for data: {self.path}", fg=WARN)
                return
            sensors = data["sensors"]
            names = sorted(sensors, key=sort_key)
            if names != self.names:                    # other sensors, or a real resize
                self.build(names)
            # The logger rewrites the whole file on every report and at least once per
            # --interval. An old file means the logger stopped or is not writing it, so
            # everything on screen is history: say so instead of showing it as current.
            file_age = age_s(data.get("generated_utc"))
            frozen = file_age > STALE_FILE_S
            self.load_corrections()
            any_corrected = False
            for name in names:
                s, lbl = sensors[name], self.cards.get(name)
                if lbl is None:
                    continue
                t, hum, bat = s.get("temperature_c"), s.get("humidity_pct"), s.get("battery_pct")
                add_t, add_h = self.corr.get(s.get("sensor_id") or "", (0.0, 0.0))
                mark_t = "*" if add_t and t is not None else ""
                mark_h = "*" if add_h and hum is not None else ""
                if mark_t:
                    t += add_t
                if mark_h:
                    hum += add_h
                any_corrected = any_corrected or bool(mark_t or mark_h)
                offline = s.get("reachable") is False
                reported = s.get("last_report_utc")
                # No report yet only means this sensor has been quiet since the logger
                # started - the value is the hub's current one, so do not grey it out.
                quiet = age_s(reported) if reported else 0.0
                late = quiet > HEARTBEAT_S + TOL_S
                silent = quiet > 2 * HEARTBEAT_S + TOL_S
                stale = offline or frozen or silent
                self.put(lbl["temp"], text="--" if t is None else f"{t:.1f}\u00b0{mark_t}",
                         fg=STALE if stale else FG)
                self.put(lbl["hum"], text="--" if hum is None else f"{hum:.1f} % RH{mark_h}")
                if offline:
                    foot = "offline"
                elif late:
                    m = int(quiet // 60)
                    foot = f"{'silent' if silent else 'late'} {m // 60}h{m % 60:02d}" if m >= 60 \
                        else f"late {m} min"
                else:
                    foot = local_hm(reported) if reported else "\u2014"
                if bat is not None:
                    foot = f"bat {bat:.0f}%   {foot}"
                warn = offline or late or (bat is not None and bat < 20)
                self.put(lbl["foot"], text=foot, fg=WARN if warn else DIM)
            age = ("" if file_age < 120 else
                   f" ({file_age / 60:.0f} min old!)" if file_age < 5400 else
                   f" ({file_age / 3600:.0f} h old!)")
            corr_note = (f"   {self.corr_error}" if self.corr_error else
                         "   * corrected" if any_corrected else "")
            self.put(self.status,
                     text=f"{len(names)} sensors{corr_note}   {datetime.now():%H:%M}   "
                          f"data {local_hm(data.get('generated_utc'))}{age}",
                     fg=WARN if frozen or self.corr_error else DIM)
        finally:
            self.root.after(REFRESH_MS, self.refresh)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("path", nargs="?", default="/opt/aqara/app/latest.json")
    p.add_argument("--windowed", metavar="WxH", help="window instead of fullscreen, e.g. 800x480")
    p.add_argument("--corrections", nargs="?", const="/opt/aqara/app/corrections.json",
                   metavar="FILE", help="apply per-sensor corrections "
                   "(default file: /opt/aqara/app/corrections.json)")
    args = p.parse_args()

    root = tk.Tk()
    root.title("Aqara sensors")
    if args.windowed:
        root.geometry(args.windowed)
    else:
        # an explicit screen-sized geometry as well: without it, a desktop that ignores
        # the fullscreen request lets the window follow its contents and resize
        root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
        root.attributes("-fullscreen", True)
        root.configure(cursor="none")
    root.pack_propagate(False)                         # the contents never size the window
    root.bind("<Escape>", lambda _e: root.destroy())
    dash = Dashboard(root, Path(args.path),
                     Path(args.corrections) if args.corrections else None)
    root.bind("<Configure>", dash.on_configure)
    root.mainloop()


if __name__ == "__main__":
    main()
