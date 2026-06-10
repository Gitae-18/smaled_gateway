#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Optional

CACHE_PATH_DEFAULT = "/home/pi/config/env_latest.json"

def now_ts() -> int:
    return int(time.time())

def atomic_write_json(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def extract_json_object(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    i = line.find("{")
    j = line.rfind("}")
    if i < 0 or j < 0 or j <= i:
        return None
    chunk = line[i:j+1]
    try:
        data = json.loads(chunk)
    except Exception:
        return None
    return data if isinstance(data, dict) else None

def ensure_pigpio_free():
    # 1) pigpiod 떠 있으면 종료
    os.system("sudo -n pkill -x pigpiod >/dev/null 2>&1 || true")
    # 2) pid 파일 찌꺼기 제거
    os.system("sudo -n rm -f /var/run/pigpio.pid >/dev/null 2>&1 || true")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gwid", default=os.getenv("GWID", "gw001"))
    ap.add_argument("--multisensor", default=os.getenv("MULTISENSOR_PATH", "/home/pi/multisensor"))
    ap.add_argument("--cache-path", default=os.getenv("ENV_LATEST_PATH", CACHE_PATH_DEFAULT))
    args = ap.parse_args()

    stop = False
    def _sig(_s, _f):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    
    ensure_pigpio_free()

    proc = subprocess.Popen(
        ["/usr/bin/sudo", "-n", args.multisensor],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # 섞여 와도 JSON만 추출해서 저장
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    assert proc.stdout is not None
    print(f"[env] multisensor started pid={proc.pid}", file=sys.stderr)

    last_print = 0.0

    while not stop:
        line = proc.stdout.readline()
        if not line:
            break

        j = extract_json_object(line)
        if not j:
            print(line.rstrip(), flush=True)
            continue

        # multisensor가 찍는 env JSON만 저장 (원하면 이 필터 제거 가능)
        if j.get("t") != "gw_env":
            continue

        j.setdefault("gwid", args.gwid)
        j.setdefault("ts", now_ts())

        atomic_write_json(args.cache_path, j)

        now = time.time()
        if now - last_print >= 5.0:
            print(f"[env] saved -> {args.cache_path} keys={list(j.keys())}", file=sys.stderr)
            last_print = now

    try:
        proc.terminate()
    except Exception:
        pass

    print("[env] stopped", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
