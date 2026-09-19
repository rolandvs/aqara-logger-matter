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
STALE_S = 3 * 3600          # no value received for 3 h -> sensor shown grey
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
        self.size = (0, 0)              # last geometry we built for; see on_resize in main()
        self.cards: dict[str, dict[str, tk.Label]] = {}
        root.configure(bg=BG)
        self.status = tk.Label(root, bg=BG, fg=DIM, anchor="e", padx=10)
        self.status.pack(side="bottom", fill="x")
        self.grid = tk.Frame(root, bg=BG)
        self.grid.pack(side="top", fill="both", expand=True)
        root.after(300, self.refresh)   # let fullscreen geometry settle first

    @staticmethod
    def put(lbl: tk.Label, **kw) -> None:
        """Configure only the options that actually differ.

        Tk on X11 has no double buffering: every configure() clears the widget
        and redraws it, even when nothing changed. At one refresh every 5 s that
        is a visible flash on each tile, so skip the call when the value is the
        same as what is already on screen.
        """
        changed = {k: v for k, v in kw.items() if lbl.cget(k) != v}
        if changed:
            lbl.configure(**changed)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

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
        self.names = names
        n = max(len(names), 1)
        w, h = self.root.winfo_width(), self.root.winfo_height()
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
            short = short_name(name)
            # Fixed widths (in text units, so they scale with the font): without them
            # "--" -> "21.3°" resizes the label and pack() shifts the whole card,
            # repainting every tile instead of the one value that changed.
            # Each width is the longest text that label can show ("-12.5°*",
            # "100.0 % RH*", "bat 100%   offline"), so nothing needs more room than before.
            labels = {
                "name": tk.Label(card, text=short, bg=CARD, fg=DIM,
                                 font=("DejaVu Sans", name_px(short))),
                "temp": tk.Label(card, bg=CARD, fg=FG, font=f_temp, width=7),
                "hum":  tk.Label(card, bg=CARD, fg=FG, font=f_sub, width=11),
                "foot": tk.Label(card, bg=CARD, fg=DIM, font=f_sub, width=18),
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
            if names != self.names:
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
                stale = (offline or frozen
                         or (reported is not None and age_s(reported) > STALE_S))
                self.put(lbl["temp"], text="--" if t is None else f"{t:.1f}\u00b0{mark_t}",
                         fg=STALE if stale else FG)
                self.put(lbl["hum"], text="--" if hum is None else f"{hum:.1f} % RH{mark_h}")
                foot = "offline" if offline else (local_hm(reported) if reported else "\u2014")
                if bat is not None:
                    foot = f"bat {bat:.0f}%   {foot}"
                self.put(lbl["foot"], text=foot,
                         fg=WARN if bat is not None and bat < 20 else DIM)
            age = ("" if file_age < 120 else
                   f" ({file_age / 60:.0f} min old!)" if file_age < 5400 else
                   f" ({file_age / 3600:.0f} h old!)")
            corr_note = (f"   {self.corr_error}" if self.corr_error else
                         "   * corrected" if any_corrected else "")
            self.put(
                self.status,
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
        root.attributes("-fullscreen", True)
        root.configure(cursor="none")
    root.bind("<Escape>", lambda _e: root.destroy())
    dash = Dashboard(root, Path(args.path),
                     Path(args.corrections) if args.corrections else None)

    def on_resize(e: tk.Event) -> None:
        """Rebuild only on a real size change.

        <Configure> also fires on moves and restacks, and build() destroys and
        recreates every widget - the most visible flash of all.
        """
        if e.widget is root and (e.width, e.height) != dash.size:
            dash.size = (e.width, e.height)
            dash.names = None       # force a rebuild at the new geometry

    root.bind("<Configure>", on_resize)
    root.mainloop()


if __name__ == "__main__":
    main()
