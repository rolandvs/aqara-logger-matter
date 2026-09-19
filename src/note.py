#!/usr/bin/env python3
"""
note.py - add a time-stamped remark to the sensor log, e.g. "moved 3 to the bedroom".

    aqara-note "all sensors in the fridge"               # all sensors, now
    aqara-note -s 3 "moved to the bedroom"               # one sensor: name, start of name, or id
    aqara-note --at "2026-09-18 23:15" "fridge test"     # local time, after the fact
    aqara-note --at -20m "window opened"                 # 20 minutes ago (also -2h, -1d)
    aqara-note --list                                    # all remarks
    aqara-note --list 5                                  # the last 5

Every remark starts a new phase for the sensors it applies to, which lasts until the
next remark for them. export.py shows the phase next to the readings and can leave
phases out or keep only some (--skip / --only). So describe the new situation in each
remark ("back in the living room"), and the phases follow by themselves.

Remarks go to remarks.csv next to the sensor log; the log itself is never touched.
A per-sensor remark stores the sensor's stable id, so it stays attached to the right
sensor after renaming.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

APP = Path(os.environ.get("AQARA_DIR", "/opt/aqara/app"))
FIELDS = ["timestamp_utc", "sensor", "remark"]


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
    try:
        return ZoneInfo(key or "UTC")
    except Exception:
        print(f"Unknown time zone {key!r}, using UTC (pass --tz Europe/Amsterdam)",
              file=sys.stderr)
        return ZoneInfo("UTC")


def load_names(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items() if not k.startswith("_")}


def resolve_sensor(token: str, names: dict[str, str]) -> str:
    """Sensor id for "3", "3 [e73d]" or "lumi.54ef...". Refuses anything ambiguous."""
    if not names:
        print(f"No names file found, storing sensor {token!r} as given", file=sys.stderr)
        return token
    t = token.strip().lower()
    hits = [sid for sid, name in names.items()
            if t in (sid.lower(), name.lower()) or name.lower().startswith(t + " ")]
    if len(hits) == 1:
        return hits[0]
    options = ", ".join(sorted(names.values()))
    raise SystemExit(f"Sensor {token!r} {'is ambiguous' if hits else 'not found'}. "
                     f"Known: {options}")


def parse_when(text: str | None, tz: ZoneInfo) -> datetime:
    now = datetime.now(timezone.utc)
    if not text:
        return now
    m = re.fullmatch(r"-(\d+(?:\.\d+)?)\s*([mhd])", text.strip())
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return now - timedelta(**{unit: float(m.group(1))})
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":                        # today, local
            t = datetime.combine(datetime.now(tz).date(), t.time())
        when = t.replace(tzinfo=tz).astimezone(timezone.utc)
        if when > now + timedelta(minutes=1):
            raise SystemExit(f"{text!r} is in the future")
        return when
    raise SystemExit(f"Cannot read time {text!r}. Use 'YYYY-MM-DD HH:MM', 'HH:MM' "
                     f"(today) or a relative time like -20m, -2h, -1d")


def read_remarks(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("timestamp_utc")]
    return sorted(rows, key=lambda r: r["timestamp_utc"])


def main() -> None:
    p = argparse.ArgumentParser(description="Add a time-stamped remark to the sensor log")
    p.add_argument("text", nargs="*", help="the remark")
    p.add_argument("-s", "--sensor", help="only for this sensor (name, start of name, or id)")
    p.add_argument("--at", metavar="TIME",
                   help="when: 'YYYY-MM-DD HH:MM' or 'HH:MM' (local), or -20m / -2h / -1d")
    p.add_argument("--list", nargs="?", type=int, const=0, metavar="N",
                   help="show remarks (the last N) and exit")
    p.add_argument("--file", type=Path, default=APP / "remarks.csv")
    p.add_argument("--names", type=Path, default=APP / "names.json")
    p.add_argument("--tz", help="time zone, e.g. Europe/Amsterdam (default: the system's)")
    # "--at -20m": argparse would take -20m for an option, so glue it on as --at=-20m
    argv, fixed = sys.argv[1:], []
    while argv:
        a = argv.pop(0)
        if a == "--at" and argv and argv[0].startswith("-"):
            a = f"--at={argv.pop(0)}"
        fixed.append(a)
    args = p.parse_args(fixed)

    tz = local_zone(args.tz)
    names = load_names(args.names)

    if args.list is not None:
        rows = read_remarks(args.file)
        if args.list:
            rows = rows[-args.list:]
        if not rows:
            print(f"No remarks in {args.file}")
        for r in rows:
            t = datetime.fromisoformat(r["timestamp_utc"]).astimezone(tz)
            who = names.get(r["sensor"], r["sensor"]) if r["sensor"] else "all"
            print(f"{t:%Y-%m-%d %H:%M}  {who:<10}  {r['remark']}")
        return

    text = " ".join(args.text).strip()
    if not text:
        p.error("give the remark text, e.g.: aqara-note \"moved 3 to the bedroom\"")
    sensor = resolve_sensor(args.sensor, names) if args.sensor else ""
    when = parse_when(args.at, tz)

    new_file = not args.file.exists() or args.file.stat().st_size == 0
    try:
        with args.file.open("a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({"timestamp_utc": when.isoformat(timespec="seconds"),
                        "sensor": sensor, "remark": text})
    except PermissionError:
        raise SystemExit(f"No permission to write {args.file}. Run: "
                         f"sudo chown $USER {args.file}   (once), or use sudo")
    who = names.get(sensor, sensor) if sensor else "all sensors"
    print(f"{when.astimezone(tz):%Y-%m-%d %H:%M}  {who}: {text}")


if __name__ == "__main__":
    main()
