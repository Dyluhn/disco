"""BP-16 marathon: generate sensor_readings.csv from REAL host measurements.

Provenance: live sampling of this workstation's /sys/class/hwmon temperature
inputs (nvme, amdgpu, k10temp, spd5118 DIMM sensors) — one row per sensor per
tick, ~0.35s tick, until 200 rows. Real measurements, not synthetic (BP-16
prohibits lorem-ipsum data). Run: python make_csv.py
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
import time

HWMON = pathlib.Path("/sys/class/hwmon")
TARGET_ROWS = 200


def discover() -> list[tuple[str, pathlib.Path]]:
    sensors: list[tuple[str, pathlib.Path]] = []
    for hw in sorted(HWMON.glob("hwmon*")):
        name = (hw / "name").read_text().strip()
        for t in sorted(hw.glob("temp*_input")):
            label_f = hw / t.name.replace("_input", "_label")
            label = label_f.read_text().strip() if label_f.exists() else t.name
            sensors.append((f"{name}/{label}", t))
    return sensors


def main() -> None:
    sensors = discover()
    assert sensors, "no hwmon temperature inputs found"
    rows: list[dict] = []
    while len(rows) < TARGET_ROWS:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
        for sid, path in sensors:
            if len(rows) >= TARGET_ROWS:
                break
            try:
                milli = int(path.read_text().strip())
            except OSError:
                continue
            rows.append({"timestamp": now, "sensor": sid, "temperature_c": milli / 1000.0})
        time.sleep(0.35)

    out = pathlib.Path(__file__).parent / "sensor_readings.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "sensor", "temperature_c"])
        w.writeheader()
        w.writerows(rows)

    temps = [r["temperature_c"] for r in rows]
    prov = {
        "source": "live /sys/class/hwmon temperature sampling on the dev workstation",
        "sensors": sorted({r["sensor"] for r in rows}),
        "rows": len(rows),
        "sampled_at_utc": rows[0]["timestamp"] + " .. " + rows[-1]["timestamp"],
        "stats": {"count": len(temps), "min": min(temps), "max": max(temps)},
    }
    (pathlib.Path(__file__).parent / "provenance.json").write_text(json.dumps(prov, indent=2))
    print(json.dumps(prov["stats"]))
    print("wrote", out)


if __name__ == "__main__":
    main()
