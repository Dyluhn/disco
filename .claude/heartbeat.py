#!/usr/bin/env python3
import argparse
import datetime as dt
import pathlib
import time

parser = argparse.ArgumentParser()
parser.add_argument("--interval", type=int, default=600)
parser.add_argument("--log", default=".claude/heartbeat.log")
args = parser.parse_args()

log = pathlib.Path(args.log)
log.parent.mkdir(parents=True, exist_ok=True)

while True:
    now = dt.datetime.now(dt.UTC).isoformat()
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{now} HEARTBEAT_DUE\n")
        f.flush()
    time.sleep(args.interval)
