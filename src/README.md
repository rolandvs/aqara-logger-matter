# Aqara T1 climate logger

Logs Aqara T1 temperature/humidity sensors from an Aqara Hub M100 to CSV, locally,
over Matter. Shows them on a screen, exports time-aligned tables for charts, and
calibrates the sensors against each other.

```
T1 sensors --Zigbee--> Hub M100 --Matter--> Matter server --> logger --> sensor_log.csv
                                            (Docker)          (Docker)    latest.json --> screen
```

## What you need

- Aqara Hub M100 with T1 sensors, set up in the Aqara Home app.
- Raspberry Pi 4 with Raspberry Pi OS **64-bit, with desktop**, and a display.
  On the same network as the hub. IPv6 must be on (it is by default).

Without a screen, `install.sh` does the same on any Debian/Ubuntu machine, minus the dashboard.

## Install

1. Flash Raspberry Pi OS (64-bit, with desktop), boot, connect it to the network.
2. Copy this folder to the Pi, then:
   ```
   cd aqara-logger
   sudo bash install_pi.sh
   sudo reboot
   ```
3. Pair the hub. In Aqara Home: Hub M100 → Settings → **Expose to Matter**, enable the
   sensors, get a pairing code. Then:
   ```
   sudo aqara-commission <code>
   ```
   Use the code within a few minutes. Don't press the button on the hub: holding it
   resets the hub's network.
4. Name the sensors. Within seconds `/opt/aqara/app/names.json` lists every sensor
   by its fixed id. Change the values to your own names:
   ```
   sudo nano /opt/aqara/app/names.json
   ```
   The new names are used within seconds and stay attached to the sensor, even after re-pairing.

Optional: for an always-on screen, `sudo raspi-config` → Display Options → Screen Blanking → No.

## Use

**Screen.** Per sensor: temperature, humidity, battery, time of its last report.
Grey value: no report for 3 hours, or the hub says the sensor is unreachable.
Orange status bar: the logger stopped writing. `*`: calibrated value.

**Remarks.** Time-stamped notes, used to select data later.
Each remark starts a phase that lasts until the next remark.
```
aqara-note "all sensors together in the living room"
aqara-note -s 3 "moved to the bedroom"               # one sensor
aqara-note --at "2026-09-18 23:15" "fridge test"     # after the fact; also --at -20m
aqara-note --list
```

**Export for charts.** One column per sensor on a common time grid.
The file goes to the current folder, or to your home folder if you can't write there.
Open it in Numbers or Excel, select everything, insert a line chart.
```
aqara-export                                  # all sensors, 15 min steps, °C and %RH
aqara-export --since 2026-10-01 --metric temp --step 5
aqara-export overlay                          # week over week, hourly
aqara-export overlay --period day --sensors 3,4
aqara-export --skip fridge                    # leave out phases whose remark says "fridge"
aqara-export --nl                             # ';' and decimal comma, for Dutch Excel
```

**Calibrate.** Put all sensors together for a few hours, marked with a remark, then:
```
aqara-export calibrate --only together          # show the offsets
aqara-export calibrate --only together --write  # save them to corrections.json
```
From then on the exports and the screen apply the corrections. The log itself stays raw.
`aqara-export --raw` exports without them. Offsets smaller than their own spread are
treated as noise and not corrected.

All options: `aqara-export -h`, `aqara-note -h`.

## Reading the data

- A T1 reports only on change: about 0.5 °C, and larger steps for humidity.
  No new report means no change, not a fault.
- In `sensor_log.csv`, `update` rows are real reports. `start` and `interval` rows
  (every 5 minutes) repeat the last known values.
- Battery % drops in the cold and recovers when warm. The mV column is a fixed 3000.
- Calibration makes the sensors agree with each other, not with the true value.
  That needs an outside reference, such as ice water at 0 °C.

## Files

| Path | Content |
|---|---|
| `/opt/aqara/app/sensor_log.csv` | every report, raw, never edited |
| `/opt/aqara/app/latest.json` | current values, read by the screen |
| `/opt/aqara/app/names.json` | sensor id → your name |
| `/opt/aqara/app/remarks.csv` | remarks from `aqara-note` |
| `/opt/aqara/app/corrections.json` | calibration from `aqara-export calibrate --write` |
| `/opt/aqara/data/` | Matter pairing. Lose it and you must pair again |
| `/opt/aqara/compose.yaml` | the Matter server and logger containers |
| `/opt/aqara/dashboard.py` | the screen, started by `/etc/xdg/autostart/aqara-dashboard.desktop` |

Back up `/opt/aqara/app/` now and then. The log is the one thing you can't recreate.

## Troubleshooting

| Symptom | Check |
|---|---|
| A sensor is missing | Enabled under Expose to Matter in Aqara Home? `sudo aqara-logger --dump` lists what the hub sends |
| Values never change | `sudo aqara-logger --watch` shows every report live, and which sensor it goes to |
| Pairing times out | Pi and hub on the same network, no guest Wi-Fi or VLAN between them, IPv6 on |
| Orange "… min old!" on the screen | `sudo docker compose -f /opt/aqara/compose.yaml ps`: is the logger running? |
| Sensor goes quiet in a fridge or metal box | The radio signal is blocked. Its tile keeps showing the last value it sent |
| "No permission" from `aqara-export` or `aqara-note` | The message gives the one-line fix |
