#!/usr/bin/env python3
"""
health.py - are the sensors working? One line per sensor, from the log.

    aqara-health                           # last 24 hours
    aqara-health --hours 168               # last week
    aqara-health --at "2026-09-19 12:00"   # judge the log as it was at that moment

A T1 checks in about every 55 minutes even when nothing changes (measured from the
log itself; any change report restarts that clock). A sensor silent for longer than
one check-in plus 5 minutes has missed one. Missed check-ins are the practical measure
of its radio link: Matter carries no signal strength (RSSI/LQI) for sensors behind a
bridge, so this is the closest thing available.

Not held against a sensor:
  - time the logger itself was not running or restarting, and
  - time ALL sensors were silent together: that points at the hub or the network.

Status now:
  ok       reported within one check-in
  LATE     one check-in missed
  SILENT   two or more missed
  OFFLINE  the hub itself says it cannot reach the sensor

A check-in whose values are all identical to the previous report is not passed on
by Matter (only changes are). With values to 0.01 that is rare, but it means one
isolated miss is not yet a fault; a pattern of misses is.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

APP = Path(os.environ.get("AQARA_DIR", "/opt/aqara/app"))
DEFAULT_HB = 55 * 60      # check-in interval if the log is too short to measure it
TOL = 5 * 60              # allowed lateness of a check-in
LOGGER_GAP = 12 * 60      # logger writes a snapshot every 5 min; longer without rows = down


def ago(seconds: float) -> str:
    m = int(round(seconds / 60))
    if m < 60:
        return f"{m} min"
    if m < 48 * 60:
        return f"{m // 60} h {m % 60:02d}"
    return f"{m / 1440:.1f} d"


def local(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%a %H:%M")


def spans_overlap(a: float, b: float, spans: list[tuple[float, float]]) -> bool:
    return any(s < b and a < e for s, e in spans)


def gaps_over(times: list[float], limit: float) -> list[tuple[float, float]]:
    return [(a, b) for a, b in zip(times, times[1:]) if b - a > limit]


def observed(a: float, b: float, spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of (a, b) outside the given spans (times nobody was watching)."""
    pieces, cur = [], a
    for s, e in sorted(spans):
        if e <= cur or s >= b:
            continue
        if s > cur:
            pieces.append((cur, s))
        cur = max(cur, e)
    if cur < b:
        pieces.append((cur, b))
    return pieces


def main() -> None:
    p = argparse.ArgumentParser(description="Sensor health from the log",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n\n", 1)[1])
    p.add_argument("--hours", type=float, default=24, help="look back this far (24)")
    p.add_argument("--at", metavar="TIME", help="judge as of this local time, 'YYYY-MM-DD HH:MM'")
    p.add_argument("--log", type=Path, default=APP / "sensor_log.csv")
    p.add_argument("--latest", type=Path, default=APP / "latest.json")
    p.add_argument("--names", type=Path, default=APP / "names.json")
    args = p.parse_args()

    if args.at:
        now = datetime.strptime(args.at, "%Y-%m-%d %H:%M").astimezone().timestamp()
    else:
        now = datetime.now(timezone.utc).timestamp()
    start = now - args.hours * 3600

    # ---- read the log (up to "now")
    reports: dict[str, list[float]] = {}          # real reports ("update" rows)
    battery: dict[str, list[tuple[float, float]]] = {}
    names: dict[str, str] = {}
    all_rows: list[float] = []
    starts: set[float] = set()                    # logger (re)starts
    try:
        f = args.log.open(encoding="utf-8", newline="")
    except FileNotFoundError:
        raise SystemExit(f"{args.log} not found")
    with f:
        reader = csv.DictReader(f)
        has_id = "sensor_id" in (reader.fieldnames or [])
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp_utc"]).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            if ts > now:
                continue
            key = ((row.get("sensor_id") if has_id else row.get("sensor")) or "").strip()
            if not key:
                continue                           # rows from before sensor ids existed
            all_rows.append(ts)
            names[key] = row.get("sensor") or key
            if row.get("trigger") == "update":
                reports.setdefault(key, []).append(ts)
            elif row.get("trigger") == "start":
                starts.add(ts)
            try:
                battery.setdefault(key, []).append((ts, float(row["battery_pct"])))
            except (KeyError, TypeError, ValueError):
                pass
    if not all_rows:
        raise SystemExit(f"No sensor rows in {args.log}")
    try:
        names.update({k: str(v) for k, v in json.loads(args.names.read_text("utf-8")).items()
                      if not k.startswith("_")})
    except (OSError, ValueError):
        pass
    reachable: dict[str, bool | None] = {}
    if not args.at:                                # the hub's verdict is only known for now
        try:
            for s in json.loads(args.latest.read_text("utf-8")).get("sensors", {}).values():
                if s.get("sensor_id"):
                    reachable[s["sensor_id"]] = s.get("reachable")
        except (OSError, ValueError):
            pass
    all_rows.sort()
    for v in reports.values():
        v.sort()

    # ---- check-in interval, measured: the typical gap between reports near 55 min
    near = [b - a for v in reports.values() for a, b in zip(v, v[1:])
            if b > start and 45 * 60 <= b - a <= 65 * 60]
    hb = statistics.median(near) if len(near) >= 5 else DEFAULT_HB
    hb_note = (f"{hb / 60:.1f} min, measured ({len(near)} samples)" if len(near) >= 5
               else f"{hb / 60:.0f} min, default (too few samples to measure)")

    # ---- when was the logger down, and when were all sensors quiet together?
    down = gaps_over(all_rows, LOGGER_GAP)
    # A restart is a short outage too: a check-in arriving while the logger restarts is
    # not logged as a report (its values only show up in the "start" snapshot). The
    # logger was not listening at some point between its last row and the restart.
    for s in sorted(starts):
        i = bisect.bisect_left(all_rows, s)
        if i > 0 and all_rows[i - 1] < s:
            down.append((all_rows[i - 1], s))
    logger_age = now - all_rows[-1]
    logger_down_now = logger_age > LOGGER_GAP
    if logger_down_now:
        down.append((all_rows[-1], now))
    union = sorted(t for v in reports.values() for t in v)
    hub_quiet = [(a, b) for a, b in gaps_over(union + [now], hb + TOL)
                 if b > start and not spans_overlap(a, b, down)]

    # ---- per sensor
    rows_out = []
    problems = []
    for key in sorted(names, key=lambda k: names[k]):
        v = reports.get(key, [])
        before = [t for t in v if t <= start][-1:]
        inside = [t for t in v if t > start]
        seq = before + inside
        missed, longest = 0, 0.0
        for a, b in zip(seq, seq[1:] + [now]):
            # Only count what was observed: while the logger was down or the whole hub was
            # quiet, this sensor cannot be judged. After such a stretch it gets the benefit
            # of the doubt, as if it reported the moment observation resumed.
            for p0, p1 in observed(a, b, down + hub_quiet):
                longest = max(longest, p1 - max(p0, start))
                k = 1
                while p0 + k * hb + TOL < p1:      # each check-in due in this stretch
                    if p0 + k * hb >= start:
                        missed += 1
                    k += 1
        last = v[-1] if v else None
        age = now - last if last else math.inf
        if logger_down_now:
            status = "unknown"
        elif reachable.get(key) is False:
            status = "OFFLINE"
        elif age <= hb + TOL:
            status = "ok"
        elif age <= 2 * hb + TOL:
            status = "LATE"
        else:
            status = "SILENT"
        bats = [b for t, b in battery.get(key, []) if t > start]
        bat = (f"{bats[-1]:.0f}%" + (f" ({min(bats):.0f}-{max(bats):.0f})"
                                     if max(bats) - min(bats) >= 1 else "")) if bats else "-"
        when = f"{local(last)} ({ago(age)} ago)" if last else "never"
        rows_out.append((names[key], when, status, len(inside), missed,
                         ago(longest) if longest else "-", bat))
        n = names[key]
        if status == "OFFLINE":
            problems.append(f"{n}: the hub reports it unreachable. Battery, or out of radio range.")
        elif status == "SILENT":
            problems.append(f"{n}: nothing for {ago(age)}. Battery, range, or the sensor "
                            f"sits in a metal box or fridge.")
        elif status == "LATE":
            problems.append(f"{n}: one check-in overdue. Wait for the next one before worrying.")
        # One isolated miss can be a check-in with unchanged values, which Matter does not
        # pass on. Only a pattern says something about the radio link.
        if missed >= 2 and status in ("ok", "LATE"):
            problems.append(f"{n}: {missed} missed check-ins in the window. A few means a "
                            f"marginal radio link (distance, walls, metal); many means a weak one.")
        if bats and min(bats) <= 10:
            problems.append(f"{n}: battery reading dropped to {min(bats):.0f}%. Dips toward zero "
                            f"mean the cell cannot hold its voltage while transmitting: replace it.")
        elif bats and bats[-1] < 20:
            problems.append(f"{n}: battery {bats[-1]:.0f}%: replace soon.")

    # ---- report
    state = (f"NOT WRITING for {ago(logger_age)}" if logger_down_now
             else f"running (last row {ago(logger_age)} ago)")
    print(f"Logger {state}.   Window: last {ago(args.hours * 3600)}, since {local(start)}.")
    print(f"Check-in interval: {hb_note}. Late after {(hb + TOL) / 60:.0f} min of silence.\n")
    w = max(10, max(len(r[0]) for r in rows_out) + 2)
    wl = max(len(r[1]) for r in rows_out) + 2
    print(f"{'sensor':<{w}}{'last report':<{wl}}{'status':<9}{'reports':>8}{'missed':>8}"
          f"{'longest':>10}   battery")
    for n, when, status, cnt, miss, longest, bat in rows_out:
        print(f"{n:<{w}}{when:<{wl}}{status:<9}{cnt:>8}{miss:>8}{longest:>10}   {bat}")
    print()
    outside = [(a, b) for a, b in down if b > start and not (logger_down_now and b == now)]
    if outside:
        print(f"Logger stopped or restarting: {len(outside)}x, "
              f"{ago(sum(b - max(a, start) for a, b in outside))} in total (not held against the sensors)")
    if hub_quiet:
        print(f"All sensors silent together: {len(hub_quiet)}x, "
              f"{ago(sum(b - max(a, start) for a, b in hub_quiet))} in total. That is the hub, "
              f"its Wi-Fi or the Matter connection, not the sensors.")
    if logger_down_now:
        print("Nothing can be judged while the logger is not writing. Check:\n"
              "  sudo docker compose -f /opt/aqara/compose.yaml ps")
    elif problems:
        print("\n".join(problems))
    else:
        print(f"All {len(rows_out)} sensors checked in on time.")
    if any(r[6].count("(") for r in rows_out):
        print("Battery (min-max): the percentage is an estimate and moves with temperature.")


if __name__ == "__main__":
    main()
