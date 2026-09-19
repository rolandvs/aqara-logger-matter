#!/usr/bin/env python3
"""
aqara_logger.py - log Aqara T1 temperature/humidity sensors via an Aqara Hub M100.

Data path:
    T1 (Zigbee) -> Hub M100 (Matter bridge) -> OHF Matter Server (WebSocket API)
    -> this script -> CSV

Requirements : Python 3.10+,  pip install websockets
Matter server: matterjs-server (Docker), WebSocket at ws://<host>:5580/ws

Usage:
    python aqara_logger.py --commission 12345678901      # one time: add the M100 to the server
    python aqara_logger.py                               # log to sensor_log.csv
    python aqara_logger.py --url ws://192.168.1.20:5580/ws --interval 60 --out climate.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

log = logging.getLogger("aqara_logger")

# ---- Matter cluster / attribute IDs (decimal, as used in the server's "ep/cluster/attr" paths) ----
DESCRIPTOR, PARTS_LIST = 29, 3                       # 0x001D / 0x0003
POWER_SOURCE = 47                                    # 0x002F
BRIDGED_INFO = 57                                    # 0x0039 BridgedDeviceBasicInformation
TEMPERATURE, PRESSURE, HUMIDITY = 1026, 1027, 1029   # 0x0402 / 0x0403 / 0x0405

# Everything the bridge may tell us about a bridged device (cluster 57).
BRIDGED_ATTRS = {
    1: "vendor_name", 2: "vendor_id", 3: "product_name", 4: "product_id",
    5: "label",                      # NodeLabel - the name set in the Aqara app
    7: "hardware_version", 8: "hardware_version_str",
    9: "software_version", 10: "software_version_str",
    11: "manufacturing_date", 12: "part_number", 13: "product_url",
    14: "product_label", 15: "serial", 17: "reachable",
    18: "uid",                       # UniqueID - stable per bridged device
}
IDENTITY = ("uid", "serial")         # first one present is the stable identity

# (cluster, attribute) -> (CSV column, multiplier to reach the unit in the column name)
METRICS = {
    (TEMPERATURE, 0):   ("temperature_c", 0.01),  # MeasuredValue int16, 0.01 degC
    (HUMIDITY, 0):      ("humidity_pct", 0.01),   # MeasuredValue uint16, 0.01 %RH
    (PRESSURE, 0):      ("pressure_hpa", 1.0),    # MeasuredValue int16, 0.1 kPa == 1 hPa
    (POWER_SOURCE, 12): ("battery_pct", 0.5),     # BatPercentRemaining 0..200 (half percent)
    (POWER_SOURCE, 11): ("battery_mv", 1.0),      # BatVoltage, mV
}
COLUMNS = ["timestamp_utc", "trigger", "sensor", "sensor_id", "node_id",
           *(c for c, _ in METRICS.values())]
DEBOUNCE_S = 2.0  # temp + humidity arrive as separate reports; merge them into one row

NAMES_HELP = ("Edit the values to your own sensor names (e.g. \"1\" ... \"5\"). "
              "The keys are the bridge's stable device IDs - do not change them. "
              "New sensors are added here automatically; saving takes effect within seconds.")


def parse_path(path: str) -> tuple[int, int, int]:
    ep, cluster, attr = path.split("/")
    return int(ep), int(cluster), int(attr)


class SensorLogger:
    def __init__(self, url: str, out: str, interval: int, latest: str | None = None,
                 names: str | None = None) -> None:
        self.url = url
        self.out = Path(out)
        self.interval = interval
        self.latest = Path(latest) if latest else None
        self.names_file = Path(names) if names else None
        self.name_map: dict[str, str] = {}                # stable id -> name chosen by the user
        self._names_mtime: float | None = None
        self._known: set[str] = set()                     # ids already written to the names file
        self.last_report: dict[str, str] = {}             # sensor -> UTC time of last received value
        self.nodes: dict[int, dict[str, object]] = {}   # node_id -> {"ep/cluster/attr": value}
        self.seen: dict[tuple[int, str], float] = {}      # (node, path) -> time of last report
        self.pending: set[tuple[int, str]] = set()

    # ------------------------------------------------------------------ names file
    def load_names(self) -> None:
        """Re-read the names file when it changed on disk (edit it while the logger runs)."""
        if self.names_file is None:
            return
        try:
            mtime = self.names_file.stat().st_mtime
        except OSError:
            return
        if mtime == self._names_mtime:
            return
        try:
            data = json.loads(self.names_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Cannot read %s: %s", self.names_file, exc)
            return
        self._names_mtime = mtime
        self.name_map = {k: str(v).strip() for k, v in data.items()
                         if not k.startswith("_") and str(v).strip()}
        self._known |= set(self.name_map)
        log.info("Loaded %d name(s) from %s", len(self.name_map), self.names_file)

    def remember(self, found: dict[str, str]) -> None:
        """Add ids the names file doesn't have yet, with their current name as placeholder."""
        if self.names_file is None:
            return
        new = {k: v for k, v in found.items() if k not in self._known}
        if not new:
            return
        self._known |= set(new)
        data: dict[str, str] = {}
        if self.names_file.exists():
            try:
                data = json.loads(self.names_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("%s is unreadable, leaving it alone", self.names_file)
                return
        data.setdefault("_comment", NAMES_HELP)
        data.update({k: v for k, v in new.items() if k not in data})
        tmp = self.names_file.with_name(self.names_file.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.names_file)
        except OSError as exc:
            log.warning("Cannot write %s: %s", self.names_file, exc)
            return
        self._names_mtime = self.names_file.stat().st_mtime
        log.info("Added %d sensor(s) to %s: %s", len(new), self.names_file,
                 ", ".join(f"{k} = {v}" for k, v in new.items()))

    # ------------------------------------------------------------------ data model
    def load_node(self, node: dict) -> None:
        self.nodes[node["node_id"]] = dict(node.get("attributes") or {})

    def devices(self, node_id: int) -> tuple[dict[int, int], dict[int, dict]]:
        """(endpoint -> owning device endpoint, device endpoint -> info about that device).

        A bridged device owns its own endpoint plus the child endpoints in its PartsList.
        Endpoint numbers are reassigned whenever a sensor is re-paired, so the identity is
        UniqueID (or SerialNumber): "id" below. The display name is, in order of preference,
        the name from the names file, the NodeLabel set in Aqara Home, or the label plus a
        short piece of the id when several sensors share one label.
        """
        info: dict[int, dict] = {}
        parts: dict[int, list] = {}
        for path, value in self.nodes.get(node_id, {}).items():
            ep, cl, at = parse_path(path)
            if cl == BRIDGED_INFO:
                key = BRIDGED_ATTRS.get(at)
                if key and value not in (None, ""):
                    info.setdefault(ep, {})[key] = value
            elif cl == DESCRIPTOR and at == PARTS_LIST and isinstance(value, list):
                parts[ep] = value

        owner = {ep: ep for ep in info}                  # endpoints of bridged devices
        for root in info:
            for child in parts.get(root, []):
                owner.setdefault(child, root)

        labels = [str(d["label"]) for d in info.values() if d.get("label")]
        for ep, d in info.items():
            d["id"] = next((str(d[k]) for k in IDENTITY if d.get(k)), f"node{node_id}-ep{ep}")
            label = str(d.get("label") or d.get("product_label") or d.get("product_name") or "")
            short = "".join(c for c in d["id"] if c.isalnum())[-4:] or str(ep)
            if not label:
                auto = f"sensor {short}"
            elif labels.count(label) > 1:                # same name for every sensor
                auto = f"{label.split()[-1]} {short}"
            else:
                auto = label
            d["auto_name"] = auto
            d["name"] = self.name_map.get(d["id"], auto)
        return owner, info

    def readings(self, node_id: int) -> dict[str, tuple[str, dict[str, float | None]]]:
        """name -> (stable id, measured values). Only climate sensors."""
        self.load_names()
        owner, info = self.devices(node_id)
        # A device can carry the same measurement on more than one of its endpoints (a root
        # endpoint plus a child endpoint, say), and the bridge may then keep only one of them
        # up to date. Pick the endpoint that reported most recently, so a stale copy of e.g.
        # temperature cannot freeze the value. Ties: a real value beats None, then lowest ep.
        # Group by the sensor's stable id, NOT by endpoint: a bridge may split one physical
        # sensor over several endpoints (temperature on one, humidity on the next), and the
        # endpoint numbers change on re-pairing. Everything carrying the same id is one sensor.
        best: dict[str, dict[str, tuple]] = {}
        ident: dict[str, dict] = {}
        for path, value in self.nodes.get(node_id, {}).items():
            ep, cl, at = parse_path(path)
            metric = METRICS.get((cl, at))
            if metric is None:
                continue
            column, scale = metric
            d = info.get(owner.get(ep, ep), {})
            sid = str(d.get("id") or f"node{node_id}-ep{owner.get(ep, ep)}")
            ident.setdefault(sid, d)
            rank = (self.seen.get((node_id, path), 0.0), value is not None, -ep)
            current = best.setdefault(sid, {}).get(column)
            if current is None or rank > current[0]:
                best[sid][column] = (rank, None if value is None else round(value * scale, 2))
        out: dict[str, tuple[str, dict]] = {}
        for sid, cols in best.items():
            vals = {col: v for col, (_, v) in cols.items()}
            # Keep only climate sensors (drops e.g. plugs/battery-only devices on the bridge).
            if "temperature_c" not in vals and "humidity_pct" not in vals:
                continue
            name = str(ident[sid].get("name") or sid)
            while name in out:                       # two sensors given the same name
                name += f" {''.join(c for c in sid if c.isalnum())[-4:]}"
            out[name] = (sid, vals)
        out = dict(sorted(out.items()))
        self.remember({sid: name for name, (sid, _) in out.items()})
        return out

    def all_rows(self) -> list[tuple[str, int, str, dict]]:
        return [(n, nid, sid, vals)
                for nid in self.nodes for n, (sid, vals) in self.readings(nid).items()]

    def reachability(self) -> dict[str, bool]:
        out = {}
        for node_id in self.nodes:
            for d in self.devices(node_id)[1].values():
                if "reachable" in d and d.get("id"):
                    out[str(d["id"])] = bool(d["reachable"])
        return out

    # ------------------------------------------------------------------ output
    def columns(self) -> list[str]:
        """Keep the header an existing CSV already has, so older files stay readable."""
        try:
            first = self.out.open("r", encoding="utf-8").readline().strip()
        except OSError:
            return COLUMNS
        return next(csv.reader([first])) if first else COLUMNS

    def write(self, trigger: str, rows: list[tuple[str, int, str, dict]]) -> None:
        if not rows:
            return
        new_file = not self.out.exists() or self.out.stat().st_size == 0
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.out.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS if new_file else self.columns(),
                                    restval="", extrasaction="ignore")
            if new_file:
                writer.writeheader()
            for name, node_id, sensor_id, values in rows:
                writer.writerow({"timestamp_utc": ts, "trigger": trigger, "sensor": name,
                                 "sensor_id": sensor_id, "node_id": node_id, **values})
                log.info("%-8s %-24s %s", trigger, name, values)
                if trigger != "interval":
                    self.last_report[sensor_id] = ts
        self.write_latest(ts)

    def write_latest(self, ts: str) -> None:
        """Current values of all sensors as JSON (for the display). Atomic replace."""
        if self.latest is None:
            return
        reachable = self.reachability()
        data = {"generated_utc": ts, "sensors": {
            name: {**values, "node_id": nid, "sensor_id": sid,
                   "reachable": reachable.get(sid),
                   "last_report_utc": self.last_report.get(sid)}
            for name, nid, sid, values in self.all_rows()}}
        tmp = self.latest.with_name(self.latest.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.latest)

    def schedule(self, node_id: int, sensor_id: str) -> None:
        key = (node_id, sensor_id)
        if key not in self.pending:
            self.pending.add(key)
            asyncio.get_running_loop().call_later(DEBOUNCE_S, self.flush, key)

    def flush(self, key: tuple[int, str]) -> None:
        self.pending.discard(key)
        node_id, sensor_id = key
        for name, (sid, values) in self.readings(node_id).items():
            if sid == sensor_id:
                self.write("update", [(name, node_id, sid, values)])
                return

    # ------------------------------------------------------------------ server messages
    def handle(self, msg: dict) -> None:
        if "event" in msg:
            event, data = msg["event"], msg.get("data")
            if event == "attribute_updated":
                node_id, path, value = data
                if node_id not in self.nodes:
                    return
                self.nodes[node_id][path] = value
                self.seen[(node_id, path)] = time.time()
                ep, cl, at = parse_path(path)
                if (cl, at) in METRICS:
                    owner, info = self.devices(node_id)
                    dev = owner.get(ep, ep)
                    self.schedule(node_id, info.get(dev, {}).get("id", f"node{node_id}-ep{dev}"))
            elif event in ("node_added", "node_updated"):
                self.load_node(data)
            elif event == "node_removed":
                self.nodes.pop(data, None)
            elif event == "server_shutdown":
                log.warning("Matter server is shutting down")
        elif msg.get("message_id") == "listen":
            if "error_code" in msg:
                raise RuntimeError(f"start_listening failed: {msg['error_code']} {msg.get('details')}")
            for node in msg["result"]:
                self.load_node(node)
            rows = self.all_rows()
            if not rows:
                log.warning("No temperature/humidity endpoints found. Are the T1 sensors "
                            "enabled in the M100's Matter bridge settings?")
            log.info("Found %d sensor(s): %s", len(rows), ", ".join(r[0] for r in rows))
            if self.names_file:
                log.info("Names can be changed in %s", self.names_file)
            self.write("start", rows)
        elif "fabric_id" in msg:
            log.info("Connected to Matter server (schema %s)", msg.get("schema_version"))

    async def snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self.write("interval", self.all_rows())

    async def run(self) -> None:
        delay = 1
        while True:
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=30) as ws:
                    await ws.send(json.dumps({"message_id": "listen", "command": "start_listening"}))
                    delay = 1
                    snap = asyncio.create_task(self.snapshot_loop()) if self.interval > 0 else None
                    try:
                        async for raw in ws:
                            self.handle(json.loads(raw))
                    finally:
                        if snap:
                            snap.cancel()
                log.warning("Connection closed by server")
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("Connection problem: %s", exc)
            log.info("Reconnecting in %ds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def commission(url: str, code: str) -> None:
    """Add the M100 bridge (already on the LAN) to the Matter server's fabric."""
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({
            "message_id": "commission",
            "command": "commission_with_code",
            "args": {"code": code, "network_only": True},
        }))
        log.info("Commissioning... (can take a minute)")
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("message_id") != "commission":
                continue
            if "error_code" in msg:
                raise SystemExit(f"Commissioning failed: {msg['error_code']} {msg.get('details')}")
            log.info("Commissioned: node_id %s", msg["result"].get("node_id"))
            return


# The hub's own health, if it publishes it (standard Matter clusters on the hub node).
# Matter has no cluster for the radio link of a device *behind* a bridge, so per-sensor
# RSSI/LQI can only appear in an Aqara-specific (vendor) cluster, shown raw below.
HUB_DIAG = {
    40: ("Basic information", {1: "vendor", 3: "product", 10: "firmware", 15: "serial"}),
    51: ("General diagnostics", {1: "reboot count", 2: "uptime", 3: "operating hours",
                                 4: "last boot reason"}),
    54: ("Wi-Fi diagnostics", {3: "channel", 4: "RSSI (dBm)", 5: "beacons lost",
                               11: "max rate (bit/s)", 12: "overruns"}),
}
BOOT_REASONS = {0: "unspecified", 1: "power on", 2: "brown-out", 3: "software watchdog",
                4: "hardware watchdog", 5: "software update", 6: "software reset"}


def hub_value(cluster: int, attr: int, v: object) -> str:
    if v is None:
        return "-"
    if (cluster, attr) == (51, 2) and isinstance(v, (int, float)):
        return f"{v / 86400:.1f} days"
    if (cluster, attr) == (51, 4):
        return BOOT_REASONS.get(v, str(v))
    if (cluster, attr) == (54, 4) and isinstance(v, (int, float)):
        grade = "good" if v > -60 else "fair" if v > -70 else "weak"
        return f"{v} ({grade})"
    return repr(v)


async def dump(url: str, names: str | None = None, show_all: bool = False) -> None:
    """Print everything the bridge exposes: the hub's own health, then per sensor all
    identifying fields, readings, and any vendor-specific data. --all: every raw value."""
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"message_id": "listen", "command": "start_listening"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("message_id") != "listen":
                continue
            if "error_code" in msg:
                raise SystemExit(f"start_listening failed: {msg['error_code']} {msg.get('details')}")
            logger = SensorLogger(url, os.devnull, 0, names=names)
            logger.load_names()
            for node in msg["result"]:
                logger.load_node(node)
                attrs = logger.nodes[node["node_id"]]
                owner, info = logger.devices(node["node_id"])
                endpoints = sorted({parse_path(p)[0] for p in attrs})
                print(f"\n=== node {node['node_id']}  available={node.get('available')}  "
                      f"endpoints={len(endpoints)}  bridged devices={len(info)}")
                print("\n  hub (endpoint 0):")
                shown = False
                for cl, (title, fields) in HUB_DIAG.items():
                    got = [(a, attrs[f"0/{cl}/{a}"]) for a in fields if f"0/{cl}/{a}" in attrs]
                    if got:
                        shown = True
                        print(f"    {title}: " + ", ".join(
                            f"{fields[a]} {hub_value(cl, a, v)}" for a, v in got))
                if not shown:
                    print("    publishes no diagnostics")
                for ep in endpoints:
                    clusters = sorted({parse_path(p)[1] for p in attrs if parse_path(p)[0] == ep})
                    owner_ep = owner.get(ep)
                    tag = "" if owner_ep in (None, ep) else f"  (part of ep {owner_ep})"
                    shown_cl = ", ".join(str(c) if c <= 0xFFFF else f"0x{c:08X}" for c in clusters)
                    print(f"\n  ep {ep:<3} clusters [{shown_cl}]{tag}")
                    for at in sorted(BRIDGED_ATTRS):      # all identifying fields
                        path = f"{ep}/{BRIDGED_INFO}/{at}"
                        if path in attrs:
                            print(f"      {BRIDGED_ATTRS[at]:<20} = {attrs[path]!r}")
                    for (cl, at), (column, scale) in METRICS.items():
                        path = f"{ep}/{cl}/{at}"
                        if path in attrs:
                            v = attrs[path]
                            print(f"      {column:<20} = "
                                  f"{'None' if v is None else round(v * scale, 2)}")
                    for path in sorted(attrs, key=lambda p: parse_path(p)):
                        e, cl, at = parse_path(path)
                        if e != ep:
                            continue
                        if cl > 0xFFFF:                   # vendor-specific (e.g. Aqara)
                            print(f"      vendor 0x{cl:08X} attr {at:<5} = {attrs[path]!r}")
                        elif show_all:
                            print(f"      cluster {cl:<6} attr {at:<5} = {attrs[path]!r}")
                vendor = sorted({hex(parse_path(p)[1]) for p in attrs if parse_path(p)[1] > 0xFFFF})
                print(f"\n  vendor-specific clusters on this node: {', '.join(vendor) or 'none'}")
                print("\n--- sensors the logger records ---")
                for name, (sid, vals) in logger.readings(node["node_id"]).items():
                    print(f"  {name!r}  id={sid}  {vals}")
            return


async def watch(url: str, names: str | None = None) -> None:
    """Print every report as it arrives, and which sensor the logger files it under.

    Reports that all land on the same sensor name are being combined correctly. If
    temperature and humidity land on different names, the bridge splits that sensor over
    several devices and they are not being merged.
    """
    state = SensorLogger(url, os.devnull, 0, names=names)
    async with websockets.connect(url, max_size=None, ping_interval=30) as ws:
        await ws.send(json.dumps({"message_id": "listen", "command": "start_listening"}))
        print("Watching reports, Ctrl-C to stop. A quiet minute is normal: the T1 reports "
              "on change, not on a clock.\n")
        counts: dict[str, int] = {}
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("message_id") == "listen":
                for node in msg["result"]:
                    state.load_node(node)
                    nid = node["node_id"]
                    owner, info = state.devices(nid)
                    eps: dict[str, list] = {}
                    for ep in sorted({parse_path(p)[0] for p in state.nodes[nid]}):
                        sid = info.get(owner.get(ep, ep), {}).get("id")
                        if sid:
                            eps.setdefault(str(sid), []).append(ep)
                    for name, (sid, vals) in state.readings(nid).items():
                        print(f"sensor {name!r}  id={sid}  "
                              f"endpoints={eps.get(sid, [])}  {sorted(vals)}")
                print()
                continue
            if msg.get("event") in ("node_added", "node_updated"):
                state.load_node(msg["data"])
                continue
            if msg.get("event") != "attribute_updated":
                continue
            node_id, path, value = msg["data"]
            if node_id in state.nodes:
                state.nodes[node_id][path] = value
                state.seen[(node_id, path)] = time.time()
            ep, cl, at = parse_path(path)
            metric = METRICS.get((cl, at))
            what = metric[0] if metric else f"cluster {cl} attr {at}"
            shown = value if metric is None or value is None else round(value * metric[1], 2)
            counts[what] = counts.get(what, 0) + 1
            owner, info = state.devices(node_id)
            d = info.get(owner.get(ep, ep), {})
            target = f"-> {d.get('name') or '(no bridged device for this endpoint)'}"
            print(f"{datetime.now():%H:%M:%S}  node {node_id}  ep {ep:<4} {what:<16} "
                  f"{shown!s:<10} #{counts[what]:<4} {target}")


def main() -> None:
    p = argparse.ArgumentParser(description="Log Aqara T1 sensors via Hub M100 + Matter server")
    p.add_argument("--url", default="ws://localhost:5580/ws", help="Matter server WebSocket URL")
    p.add_argument("--out", default="sensor_log.csv", help="CSV output file")
    p.add_argument("--interval", type=int, default=300,
                   help="also write all last-known values every N s (0 = only on change)")
    p.add_argument("--latest", metavar="FILE", help="also keep current values in this JSON file")
    p.add_argument("--names", metavar="FILE",
                   help="JSON file mapping each sensor's stable id to your own name; "
                        "created and extended automatically, re-read while running")
    p.add_argument("--commission", metavar="CODE", help="pairing code from Aqara Home, then exit")
    p.add_argument("--dump", action="store_true",
                   help="print the hub's diagnostics and all bridged endpoints, then exit")
    p.add_argument("--all", action="store_true",
                   help="with --dump: also every raw attribute of every cluster")
    p.add_argument("--watch", action="store_true",
                   help="print every incoming sensor report live (diagnostics)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.commission:
            asyncio.run(commission(args.url, args.commission))
        elif args.dump:
            asyncio.run(dump(args.url, args.names, args.all))
        elif args.watch:
            asyncio.run(watch(args.url, args.names))
        else:
            asyncio.run(SensorLogger(args.url, args.out, args.interval,
                                     args.latest, args.names).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
