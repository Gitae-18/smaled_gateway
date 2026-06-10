import argparse
import json
import logging
import signal
import sys
import time

from cmd_router import SNAP_BIN_SIZE, T_SNAP, unpack_snap_bin


def _hex(data: bytes) -> str:
    return data.hex(" ") if isinstance(data, (bytes, bytearray)) else ""


def _extract_snap_body(payload: bytes):
    if not payload:
        return None, None

    if len(payload) >= SNAP_BIN_SIZE and payload[0] == T_SNAP:
        return bytes(payload[:SNAP_BIN_SIZE]), {
            "kind": "direct",
            "target_mid": None,
            "ttl": None,
            "cmd": None,
            "flags": None,
            "node_msg_id": None,
        }

    if len(payload) >= 7:
        body = payload[7:]
        if len(body) >= SNAP_BIN_SIZE and body[0] == T_SNAP:
            return bytes(body[:SNAP_BIN_SIZE]), {
                "kind": "transport",
                "target_mid": payload[0] | (payload[1] << 8),
                "ttl": payload[2],
                "cmd": payload[3],
                "flags": payload[4],
                "node_msg_id": (payload[5] << 8) | payload[6],
            }

    return None, None


def _snap_to_printable(mid: int, ts: int, mac: str | None, payload: bytes, body: bytes, meta: dict) -> dict:
    snap = unpack_snap_bin(body)
    if snap is None:
        return {
            "event": "snap_unpack_error",
            "mid": mid,
            "ts": ts,
            "mac": mac,
            "payload_len": len(payload),
            "payload_hex": _hex(payload),
            "body_len": len(body),
            "body_hex": _hex(body),
            "transport": meta,
        }

    return {
        "event": "snap",
        "mid": mid,
        "ts": ts,
        "mac": mac,
        "payload_len": len(payload),
        "body_len": len(body),
        "transport": meta,
        "uid": snap["uid_bytes"].hex(),
        "layout": snap.get("layout"),
        "ttl": snap.get("ttl"),
        "volt": snap.get("volt"),
        "curr": snap.get("curr"),
        "temp": snap.get("temp"),
        "light_on": snap.get("light_on"),
        "fft_count": snap.get("fft_count"),
        "fft_pairs": snap.get("fft_pairs"),
        "snap_count": snap.get("snap_count"),
        "msg_id32": snap.get("msg_id32"),
        "ok": snap.get("ok"),
        "err_code": snap.get("err_code"),
        "ai_valid": snap.get("ai_valid"),
        "ai_mse": snap.get("ai_mse"),
        "ai_pred": snap.get("ai_pred"),
        "flags": snap.get("flags"),
        "ai_raw": snap.get("ai_raw"),
        "body_tail_hex": _hex(body[-16:]),
        "body_hex": _hex(body),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Receive and decode only Wi-SUN snap packets.")
    parser.add_argument("--port", default="/dev/ttyAMA2", help="Wi-SUN UART device")
    parser.add_argument("--baud", type=int, default=9600, help="Wi-SUN UART baudrate")
    parser.add_argument("--count", type=int, default=0, help="Stop after N snap packets. 0 means forever.")
    parser.add_argument("--show-non-snap", action="store_true", help="Print non-snap payload summaries too.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    from wisun import WiSunLink

    logging.basicConfig(level=logging.WARNING)

    stop = False

    def _stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    wisun = WiSunLink(args.port, args.baud)
    print(
        f"[WISUN SNAP MONITOR] port={args.port} baud={args.baud} snap_body_size={SNAP_BIN_SIZE}",
        flush=True,
    )
    print("[WISUN SNAP MONITOR] waiting for snap packets...", flush=True)

    seen = 0
    try:
        while not stop:
            pkt = wisun.get_packet_nowait()
            if not pkt:
                time.sleep(0.02)
                continue

            if len(pkt) >= 4:
                mid, payload, ts, mac = pkt
            else:
                mid, payload, ts = pkt
                mac = None

            if not isinstance(payload, (bytes, bytearray)):
                if args.show_non_snap:
                    print(json.dumps({"event": "non_bytes", "mid": mid, "ts": ts, "payload": repr(payload)}), flush=True)
                continue

            payload = bytes(payload)
            body, meta = _extract_snap_body(payload)
            if body is None:
                if args.show_non_snap:
                    print(
                        json.dumps(
                            {
                                "event": "non_snap",
                                "mid": mid,
                                "ts": ts,
                                "mac": mac,
                                "payload_len": len(payload),
                                "p0": payload[0] if payload else None,
                                "payload_hex": _hex(payload),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                continue

            seen += 1
            obj = _snap_to_printable(mid, ts, mac, payload, body, meta)
            print(json.dumps(obj, ensure_ascii=False, indent=2 if args.pretty else None), flush=True)

            if args.count > 0 and seen >= args.count:
                break
    finally:
        wisun.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
