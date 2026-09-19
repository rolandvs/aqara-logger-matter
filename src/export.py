#!/usr/bin/env python3
"""
export.py - turn the sensor log into time-aligned, multi-column tables for charts.

The log has one row per report, at irregular times per sensor. This puts all sensors on
one time grid, so a spreadsheet can chart them directly: select everything, insert a
line chart, the first column is the time axis and every other column is a line.

Two layouts, and a calibration step:

  aligned   one row per time step, one column per sensor (and measurement), plus the
            remarks from aqara-note. For comparing sensors with each other.

  overlay   one row per time of the week (or of the day), one column per sensor per
            week (or day). Every week is laid over the others, so a repeating pattern
            shows as stacked waves and a week that behaves differently stands out.

  calibrate measures each sensor's offset from the others over a period when they were
            all in the same place, and with --write stores the corrections in
            corrections.json. Every later export applies them (unless --raw); the log
            itself is never changed, so corrections can be redone at any time.

Examples
    aqara-export                                         # aligned, 15 min, everything
    aqara-export --since "2026-09-19 00:00" --step 5 --metric temp
    aqara-export overlay                                 # week overlay, hourly, temperature
    aqara-export overlay --period day --sensors 3,4      # day overlay, two sensors
    aqara-export overlay --group period                  # all sensors side by side per week
    aqara-export --skip fridge                           # leave out phases named "...fridge..."
    aqara-export --nl                                    # ';' and decimal comma (Dutch Excel)
    aqara-export calibrate --only together               # show offsets for a marked period
    aqara-export calibrate --since "2026-09-19 01:00" --until "2026-09-19 09:45" --write
    aqara-export calibrate --only together --ref 3 --write   # align to sensor 3 instead
    aqara-export --raw                                   # without corrections

How values are made: a T1 only reports after a change (0.5 C / a few % RH), so its
value holds until the next report. Each cell is the time-weighted average of that
held value over the time step. Where the log has nothing for a sensor for longer than
--max-gap (logger stopped), the cell stays empty instead of repeating an old value.

Calibration makes the sensors agree with each other (with the median of the group, or
with --ref). Whether the group as a whole reads true needs an outside reference.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

APP = Path(os.environ.get("AQARA_DIR", "/opt/aqara/app"))
METRICS = {                       # option -> (log column, unit, decimals)
    "temp": ("temperature_c", "°C", 2),
    "rh": ("humidity_pct", "%RH", 1),
}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def warn(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- inputs
def local_zone(name: str | None) -> ZoneInfo:
    """The machine's IANA zone (not a fixed offset, so DST changes are handled)."""
    key = name or os.environ.get("TZ", "").lstrip(":")
    if not key:
        try:
            key = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
    if not key:
        target = os.path.realpath("/etc/localtime")
        key = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
    if not key:
        warn("Could not find the system time zone, using UTC (pass --tz Europe/Amsterdam)")
    try:
        return ZoneInfo(key or "UTC")
    except Exception:
        warn(f"Unknown time zone {key!r}, using UTC (pass --tz Europe/Amsterdam)")
        return ZoneInfo("UTC")


def parse_local(text: str, tz: ZoneInfo) -> float:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=tz).timestamp()
        except ValueError:
            pass
    raise SystemExit(f"Cannot read {text!r}: use YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (local)")


def natural(text: str) -> list:
    """Sort key so that "10" comes after "9"."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def read_json(path: Path) -> dict:
    """{} when the file is missing or empty. A file that exists but cannot be read is an
    error (PermissionError goes up to main), never silently treated as empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    data = json.loads(text)                     # ValueError: handled by the caller
    return data if isinstance(data, dict) else {}


def load_names(path: Path) -> dict[str, str]:
    try:
        data = read_json(path)
    except ValueError:
        warn(f"{path} is not valid JSON; using the names from the log")
        return {}
    return {k: str(v) for k, v in data.items() if not k.startswith("_")}


def load_log(path: Path, names: dict[str, str]):
    """{sensor key: {"name", "pts": {column: [(epoch, value|None)]}}}, has_id, dropped, end.

    The key is the stable sensor_id; rows logged before ids existed cannot be tied to a
    physical sensor and are left out. A log without the column falls back to names.
    """
    series: dict[str, dict] = {}
    dropped, end = 0, None
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        has_id = "sensor_id" in (reader.fieldnames or [])
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp_utc"]).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            end = ts if end is None else max(end, ts)
            key = (row.get("sensor_id") if has_id else row.get("sensor")) or ""
            key = key.strip()
            if not key:
                dropped += 1
                continue
            s = series.setdefault(key, {"name": key, "pts": {m[0]: [] for m in METRICS.values()}})
            s["name"] = (row.get("sensor") or s["name"]).strip()      # latest name wins
            for col in s["pts"]:
                raw = (row.get(col) or "").strip()
                try:
                    s["pts"][col].append((ts, float(raw) if raw else None))
                except ValueError:
                    s["pts"][col].append((ts, None))
    for key, s in series.items():
        s["name"] = names.get(key, s["name"])
        for pts in s["pts"].values():
            pts.sort(key=lambda p: p[0])
    return series, has_id, dropped, end


def load_remarks(path: Path) -> list[tuple[float, str, str]]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row["timestamp_utc"]).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            out.append((ts, (row.get("sensor") or "").strip(), (row.get("remark") or "").strip()))
    return sorted(out)


CORRECTION_FIELDS = {"temperature_c": "add_temp_c", "humidity_pct": "add_rh_pct"}
CORRECTIONS_HELP = ("Per-sensor corrections, ADDED to the measured value by aqara-export "
                    "(not by the logger: the log stays raw). Keys are the stable sensor "
                    "ids; 'name' is only for reading. Made by: aqara-export calibrate "
                    "--write, and can be edited by hand. Use aqara-export --raw to ignore.")


def load_corrections(path: Path) -> dict[str, dict[str, float]]:
    """{sensor id: {log column: amount to add}}"""
    try:
        data = read_json(path)
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")
    out = {}
    for key, entry in data.items():
        if key.startswith("_") or not isinstance(entry, dict):
            continue
        out[key] = {col: float(entry[field]) for col, field in CORRECTION_FIELDS.items()
                    if isinstance(entry.get(field), (int, float))}
    return out


def calibrate(values: dict, keys: list[str], series: dict, ref: str | None,
              min_steps: int = 8) -> dict[str, dict[str, tuple[float, float, int]]]:
    """Offset of each sensor from the reference, per measurement: (mean, spread, steps).

    Only time steps where every chosen sensor has a value count, so the comparison is
    always between readings taken at the same moment.
    """
    import statistics as st
    result: dict[str, dict] = {k: {} for k in keys}
    for col in CORRECTION_FIELDS:
        dev: dict[str, list[float]] = {k: [] for k in keys}
        n_steps = len(values[(keys[0], col)])
        for i in range(n_steps):
            row = {k: values[(k, col)][i] for k in keys}
            if any(v is None for v in row.values()):
                continue
            base = row[ref] if ref else st.median(row.values())
            for k, v in row.items():
                dev[k].append(v - base)
        n = len(dev[keys[0]])
        if n < min_steps:
            raise SystemExit(f"Only {n} time step(s) with all sensors present for "
                             f"{col}; need at least {min_steps}. Choose a longer period.")
        for k in keys:
            result[k][col] = (st.mean(dev[k]), st.pstdev(dev[k]), n)
    return result


def pick_sensors(tokens: str, series: dict) -> list[str]:
    keys = []
    for token in (t.strip().lower() for t in tokens.split(",") if t.strip()):
        hits = [k for k, s in series.items()
                if token in (k.lower(), s["name"].lower())
                or s["name"].lower().startswith(token + " ")]
        if len(hits) != 1:
            known = ", ".join(sorted((s["name"] for s in series.values()), key=natural))
            raise SystemExit(f"Sensor {token!r} {'is ambiguous' if hits else 'not found'}. "
                             f"Known: {known}")
        keys += [h for h in hits if h not in keys]
    return keys


# --------------------------------------------------------------------------- core
class Phases:
    """What was going on: the latest remark in effect for a sensor at a moment."""

    def __init__(self, remarks: list[tuple[float, str, str]]) -> None:
        self.remarks = remarks
        self._cache: dict[str | None, tuple[list, list]] = {}

    def _for(self, key: str | None) -> tuple[list, list]:
        if key not in self._cache:
            lst = [r for r in self.remarks if key is None or not r[1] or r[1] == key]
            self._cache[key] = ([r[0] for r in lst], lst)
        return self._cache[key]

    def at(self, t: float, key: str | None = None) -> tuple[str, str] | None:
        """(sensor, remark) in effect at t. key=None: any remark, for display."""
        times, lst = self._for(key)
        i = bisect.bisect_right(times, t) - 1
        return (lst[i][1], lst[i][2]) if i >= 0 else None


def binned(pts: list, start: float, step: float, nbins: int,
           max_gap: float, end: float) -> list[float | None]:
    """Time-weighted average per bin of a held (step-function) signal.

    Each report holds until the next report of that sensor, but never longer than
    max_gap: beyond that the log has nothing for it and the value is unknown.
    A bin needs at least half its length covered, or it stays empty.
    """
    sums = [0.0] * nbins
    wts = [0.0] * nbins
    stop = start + step * nbins
    for i, (t, v) in enumerate(pts):
        if v is None:
            continue
        t_next = pts[i + 1][0] if i + 1 < len(pts) else end
        a, b = max(t, start), min(t_next, t + max_gap, stop)
        k = int((a - start) // step)
        while a < b and k < nbins:
            e = min(b, start + (k + 1) * step)
            sums[k] += v * (e - a)
            wts[k] += e - a
            a, k = e, k + 1
    return [s / w if w >= 0.5 * step else None for s, w in zip(sums, wts)]


def fmt(v: float | None, decimals: int, dec: str) -> str:
    if v is None:
        return ""
    s = f"{v:.{decimals}f}"
    return s.replace(".", dec) if dec != "." else s


# --------------------------------------------------------------------------- main
def main() -> None:
    p = argparse.ArgumentParser(
        description="Export the sensor log as time-aligned columns for charts",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Examples")[1])
    p.add_argument("mode", nargs="?", choices=["aligned", "overlay", "calibrate"],
                   default="aligned")
    p.add_argument("--log", type=Path, default=APP / "sensor_log.csv")
    p.add_argument("--names", type=Path, default=APP / "names.json")
    p.add_argument("--remarks", type=Path, default=APP / "remarks.csv")
    p.add_argument("--corrections", type=Path, default=APP / "corrections.json")
    p.add_argument("--raw", action="store_true", help="ignore corrections.json")
    p.add_argument("--ref", metavar="SENSOR",
                   help="calibrate: align to this sensor instead of the group median")
    p.add_argument("--write", action="store_true",
                   help="calibrate: save the corrections to corrections.json")
    p.add_argument("-o", "--out", type=Path, help="output file (default: a name in the current dir)")
    p.add_argument("--since", help="local start, YYYY-MM-DD or 'YYYY-MM-DD HH:MM'")
    p.add_argument("--until", help="local end, same format")
    p.add_argument("--step", type=int, metavar="MIN",
                   help="minutes per row (default: 15; overlay week: 60)")
    p.add_argument("--metric", choices=["temp", "rh", "both"],
                   help="default: both (aligned), temp (overlay)")
    p.add_argument("--period", choices=["week", "day"], default="week", help="overlay: period")
    p.add_argument("--group", choices=["sensor", "period"], default="sensor",
                   help="overlay column order: a sensor's weeks together, or a week's sensors")
    p.add_argument("--sensors", help="only these, comma separated (name, start of name, id)")
    p.add_argument("--only", metavar="TEXT", help="keep only phases whose remark contains TEXT")
    p.add_argument("--skip", metavar="TEXT", help="leave out phases whose remark contains TEXT")
    p.add_argument("--max-gap", type=float, default=20, metavar="MIN",
                   help="longest a value is held without any log row for the sensor (20)")
    p.add_argument("--sep", default=",", help="column separator")
    p.add_argument("--decimal", default=".", help="decimal mark")
    p.add_argument("--nl", action="store_true", help="Dutch Excel: --sep ';' --decimal ','")
    p.add_argument("--tz", help="time zone, e.g. Europe/Amsterdam (default: the system's)")
    args = p.parse_args()

    if args.nl:
        args.sep, args.decimal = ";", ","
    if args.sep == args.decimal:
        raise SystemExit("--sep and --decimal must differ")
    overlay = args.mode == "overlay"
    calib = args.mode == "calibrate"
    if (args.ref or args.write) and not calib:
        raise SystemExit("--ref and --write belong to: aqara-export calibrate")
    metric = "both" if calib else args.metric or ("temp" if overlay else "both")
    if overlay and metric == "both":
        raise SystemExit("overlay shows one measurement at a time: --metric temp or --metric rh")
    step_min = args.step or (60 if overlay and args.period == "week" else 15)
    if overlay and 60 % step_min:
        raise SystemExit("overlay --step must divide an hour: 5, 10, 15, 20, 30 or 60")
    if step_min < 1:
        raise SystemExit("--step must be at least 1 minute")
    step = step_min * 60.0

    tz = local_zone(args.tz)
    if not args.log.exists():
        raise SystemExit(f"{args.log} not found (use --log)")
    names = load_names(args.names)
    series, has_id, dropped, data_end = load_log(args.log, names)
    if not series:
        raise SystemExit(f"No sensor rows in {args.log}")
    if not has_id:
        warn("This log has no sensor_id column: sensors are told apart by name only.")
    if dropped:
        warn(f"Left out {dropped} row(s) without sensor_id (logged before ids existed).")

    # corrections: added to every reading before anything else, never in calibrate mode
    # (offsets must be measured on raw data) and never with --raw
    corrected: list[str] = []
    if not calib and not args.raw:
        for key, adds in load_corrections(args.corrections).items():
            if key not in series or not any(adds.values()):
                continue
            for col, add in adds.items():
                series[key]["pts"][col] = [(t, None if v is None else v + add)
                                           for t, v in series[key]["pts"][col]]
            corrected.append(series[key]["name"])

    keys = (pick_sensors(args.sensors, series) if args.sensors
            else sorted(series, key=lambda k: natural(series[k]["name"])))
    ref = None
    if args.ref:
        ref = pick_sensors(args.ref, series)[0]
        if ref not in keys:
            keys.append(ref)
    cols = [METRICS[m] for m in (["temp", "rh"] if metric == "both" else [metric])]

    # time range, starting on a step boundary counted from local midnight
    first = min(p[0] for k in keys for pts in series[k]["pts"].values() for p in pts[:1])
    last = data_end
    if args.since:
        first = max(first, parse_local(args.since, tz))
    if args.until:
        last = min(last, parse_local(args.until, tz))
    if last <= first:
        raise SystemExit("No data in the chosen time range")
    local0 = datetime.fromtimestamp(first, tz)
    midnight = datetime(local0.year, local0.month, local0.day, tzinfo=tz).timestamp()
    start = midnight + math.floor((first - midnight) / step) * step
    nbins = max(1, math.ceil((last - start) / step))

    values = {(k, c[0]): binned(series[k]["pts"][c[0]], start, step, nbins,
                                args.max_gap * 60, data_end) for k in keys for c in cols}

    # phases: blank out what --only / --skip exclude, judged at the middle of each step
    remarks = load_remarks(args.remarks)
    phases = Phases(remarks)
    removed = 0
    if args.only or args.skip:
        only, skip = (args.only or "").lower(), (args.skip or "").lower()
        for k in keys:
            for i in range(nbins):
                ph = phases.at(start + (i + 0.5) * step, k)
                text = ph[1].lower() if ph else ""
                if (only and only not in text) or (skip and skip in text):
                    for c in cols:
                        if values[(k, c[0])][i] is not None:
                            values[(k, c[0])][i] = None
                            removed += 1

    if calib:
        if len(keys) < 2:
            raise SystemExit("Calibration needs at least two sensors")
        res = calibrate(values, keys, series, ref)
        n = res[keys[0]]["temperature_c"][2]
        span = (f"{datetime.fromtimestamp(start, tz):%Y-%m-%d %H:%M} - "
                f"{datetime.fromtimestamp(start + nbins * step, tz):%Y-%m-%d %H:%M}")
        basis = f"sensor {series[ref]['name']}" if ref else "the median of the group"
        filt = "".join(f", {o} {v!r}" for o, v in (("only", args.only), ("skip", args.skip)) if v)
        print(f"Offsets against {basis}, {span}{filt}, {n} x {step_min} min with all "
              f"sensors present.\nCorrection = what gets added. Spread = how much the "
              f"offset varied; an offset within its spread is noise and is not corrected.\n")
        print(f"{'sensor':<14}{'temp offset':>12}{'spread':>8}{'correction':>12}"
              f"{'RH offset':>12}{'spread':>8}{'correction':>12}")
        new: dict[str, dict] = {}

        def corr(offset: float, spread: float, digits: int) -> float:
            # an offset no bigger than its own step-to-step spread cannot be told apart
            # from noise, so it is not corrected (stored as 0 to show it was checked)
            return 0.0 if abs(offset) <= spread else round(-offset, digits) + 0.0

        def show(c: float, digits: int) -> str:
            return f"{c:>+12.{digits}f}" if c else f"{'- (noise)':>12}"

        for k in keys:
            (to, ts, _), (ho, hs, _) = res[k]["temperature_c"], res[k]["humidity_pct"]
            new[k] = {"name": series[k]["name"],
                      "add_temp_c": corr(to, ts, 2), "add_rh_pct": corr(ho, hs, 1)}
            if k == ref:
                print(f"{series[k]['name']:<14}{'reference':>12}{'':>8}{'':>12}{'reference':>12}")
                continue
            print(f"{series[k]['name']:<14}{to:>+12.2f}{ts:>8.2f}{show(new[k]['add_temp_c'], 2)}"
                  f"{ho:>+12.1f}{hs:>8.1f}{show(new[k]['add_rh_pct'], 1)}")
        if not args.write:
            print(f"\nNothing saved. Add --write to store these in {args.corrections}")
            return
        try:
            data = read_json(args.corrections)
        except ValueError as exc:
            raise SystemExit(f"{args.corrections} is not valid JSON ({exc}); not overwriting")
        data["_comment"] = CORRECTIONS_HELP
        data["_calibrated"] = f"against {basis}, {span}{filt}, {n} x {step_min} min"
        data.update(new)                   # sensors not in this calibration keep their values
        payload = json.dumps(data, indent=1, ensure_ascii=False)
        tmp = args.corrections.with_name(args.corrections.name + ".tmp")
        try:                               # atomic: readers never see half a file
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, args.corrections)
        except PermissionError:
            # The folder belongs to root, but the file itself may be ours: write in place.
            try:
                args.corrections.write_text(payload, encoding="utf-8")
            except PermissionError:
                c = args.corrections
                raise SystemExit(f"No permission to write {c}. Fix once, then no sudo needed:\n"
                                 f"  sudo touch {c} && sudo chown $USER {c}")
        print(f"\nSaved to {args.corrections}. Exports now apply these; --raw to leave them out.")
        return

    def who(sensor_key: str) -> str:
        return series[sensor_key]["name"] if sensor_key in series else names.get(sensor_key, sensor_key)

    def label(ph: tuple[str, str] | None) -> str:
        if not ph:
            return ""
        return f"{who(ph[0])}: {ph[1]}" if ph[0] else ph[1]

    d0 = datetime.fromtimestamp(start, tz)
    d1 = datetime.fromtimestamp(start + nbins * step, tz)

    if not overlay:
        header = ["time"] + [f"{series[k]['name']} {c[1]}" for c in cols for k in keys]
        header += ["remark", "phase"]
        rows = []
        r_idx = 0
        for i in range(nbins):
            t0 = start + i * step
            here = []
            while r_idx < len(remarks) and remarks[r_idx][0] < t0 + step:
                if remarks[r_idx][0] >= t0:
                    here.append(label((remarks[r_idx][1], remarks[r_idx][2])))
                r_idx += 1
            rows.append([datetime.fromtimestamp(t0, tz).strftime("%Y-%m-%d %H:%M")]
                        + [fmt(values[(k, c[0])][i], c[2], args.decimal) for c in cols for k in keys]
                        + [" | ".join(here), label(phases.at(t0 + step / 2))])
        default = f"aligned_{d0:%Y%m%d-%H%M}_{d1:%Y%m%d-%H%M}.csv"
    else:
        col, unit, dec = cols[0]
        period_min = 7 * 1440 if args.period == "week" else 1440
        cells: dict[tuple[int, str, str], list[float]] = {}
        periods: set[str] = set()
        filled: set[tuple[str, str]] = set()          # (sensor, period) with any data
        for i in range(nbins):
            local = datetime.fromtimestamp(start + i * step, tz)
            off = local.hour * 60 + local.minute          # wall clock, so DST lines up
            if args.period == "week":
                iso = local.isocalendar()
                pkey = f"{iso[0]}-W{iso[1]:02d}"
                off += local.weekday() * 1440
            else:
                pkey = local.date().isoformat()
            for k in keys:
                v = values[(k, col)][i]
                if v is not None:
                    cells.setdefault((off, k, pkey), []).append(v)   # 2 on DST-end night
                    periods.add(pkey)
                    filled.add((k, pkey))
        order = sorted(periods)
        pairs = ([(k, q) for k in keys for q in order] if args.group == "sensor"
                 else [(k, q) for q in order for k in keys])
        pairs = [pair for pair in pairs if pair in filled]
        header = [f"time of {args.period} ({unit})"] + [
            f"{series[k]['name']} {q}" if args.group == "sensor" else f"{q} {series[k]['name']}"
            for k, q in pairs]
        rows = []
        for off in range(0, period_min, step_min):
            hhmm = f"{off % 1440 // 60:02d}:{off % 60:02d}"
            row = [f"{DAYS[off // 1440]} {hhmm}" if args.period == "week" else hhmm]
            for k, q in pairs:
                vals = cells.get((off, k, q))
                row.append(fmt(sum(vals) / len(vals), dec, args.decimal) if vals else "")
            rows.append(row)
        default = (f"overlay_{args.period}_{metric}_{d0:%Y%m%d}_{d1:%Y%m%d}.csv")

    shown = [series[k]["name"] for k in keys if series[k]["name"] in corrected]
    if shown:
        default = default.replace(".csv", "_corrected.csv")
    if args.out:
        out = args.out
    else:   # current folder, or home when the current one is not ours (e.g. /opt/aqara/app)
        here = Path.cwd()
        out = (here if os.access(here, os.W_OK) else Path.home()) / default
    try:
        with out.open("w", encoding="utf-8-sig", newline="") as f:   # BOM: Excel reads UTF-8
            w = csv.writer(f, delimiter=args.sep)
            w.writerow(header)
            w.writerows(rows)
    except PermissionError:
        raise SystemExit(f"No permission to write {out}. Choose a place of your own: "
                         f"-o ~/{out.name}")

    data_cells = sum(1 for r in rows for v in r[1:len(header) - (0 if overlay else 2)] if v != "")
    total = len(rows) * (len(header) - 1 - (0 if overlay else 2))
    print(f"{out}: {len(rows)} rows x {len(header)} columns, "
          f"{d0:%Y-%m-%d %H:%M} to {d1:%Y-%m-%d %H:%M}, {step_min} min steps, "
          f"{100 * data_cells / max(total, 1):.0f}% filled", file=sys.stderr)
    if shown:
        print(f"Corrections from {args.corrections} applied to: {', '.join(shown)} "
              f"(--raw to leave them out)", file=sys.stderr)
    if removed:
        print(f"{removed} value(s) left out by --only/--skip", file=sys.stderr)
    if (args.only or args.skip) and not remarks:
        warn(f"No remarks found in {args.remarks}, so --only/--skip had nothing to match")


if __name__ == "__main__":
    try:
        main()
    except PermissionError as exc:          # an input file only root can read
        name = exc.filename or "a file"
        raise SystemExit(f"No permission to read {name}. Fix once: sudo chmod o+r {name}")
