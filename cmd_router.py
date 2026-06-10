from store import NodeStore
import time
import threading, os
import struct
import subprocess, datetime
import json, queue
import logging
import string
import os
import math
from collections import defaultdict, deque

CMD_ROUTER_VERBOSE_LOG = False

def _verbose_print(*args, **kwargs):
    if CMD_ROUTER_VERBOSE_LOG:
        print(*args, **kwargs)

CMD_SNAP = 0x13 
CMD_SET_MID_CH = 0x21
CMD_SET_SETTING = 0x31
CMD_GET_FFT   = 0x27
CMD_GET_UID   = 0x22
CMD_GET_CH    = 0x24
CMD_SET_CH    = 0x25
CMD_GET_STATUS = 0x30

CMD_GET_VI    = 0x26   
CMD_GET_INFO  = 0x40
#CMD_GET_INFO = 0x27 
#CMD_GET_NODE_INFO = 0x40
CMD_SET_POWER = 0x28  
CMD_SET_NODE_INFO      = 0x29
CMD_SET_TIME_INTERVAL  = 0x2A
CMD_SET_RTC_KST        = 0x2B

T_SNAP = 0x01
T_LIGHT_STATE_EVENT = 0x15
LIGHT_STATE_EVENT_BIN_FMT = "<B12sIBBBBIfffBffff"
LIGHT_STATE_EVENT_BIN_SIZE = struct.calcsize(LIGHT_STATE_EVENT_BIN_FMT)
LIGHT_STATE_EVENT_RTC_EXT_FMT = "<HBBBBBB"
LIGHT_STATE_EVENT_RTC_EXT_SIZE = struct.calcsize(LIGHT_STATE_EVENT_RTC_EXT_FMT)
LIGHT_STATE_EVENT_BIN_SIZE_WITH_RTC = LIGHT_STATE_EVENT_BIN_SIZE + LIGHT_STATE_EVENT_RTC_EXT_SIZE
SNAP_FFT_PAIRS = 2
# Node snap layout: t, ttl, uid, volt, curr, temp, light_on,
# fft0{uint32 freq_hz_x100, int32 amp_x1000},
# snap_count, ai_mse_x1000000, flags
SNAP_AI_MSE_SCALE = 1_000_000.0
SNAP_BIN_FMT = "<BB12sfffBIiHHB"
SNAP_BIN_SIZE = struct.calcsize(SNAP_BIN_FMT)
SNAP_POST_FREQ0_OFFSET = struct.calcsize("<BB12sfffBI")

ACK_T = 0x10
ACK_NODE_CFG_T = 0x20
ACK_BIN_FMT = "<B12sIBb"
ACK_BIN_SIZE = struct.calcsize(ACK_BIN_FMT)

STATUS_T = 0x02
STATUS_BIN_FMT_V1 = "<B12sfffBIBb"
STATUS_BIN_SIZE_V1 = struct.calcsize(STATUS_BIN_FMT_V1)
STATUS_BIN_FMT_V2 = "<B12sfffBIIBb"
STATUS_BIN_SIZE_V2 = struct.calcsize(STATUS_BIN_FMT_V2)
STATUS_BIN_SIZE = STATUS_BIN_SIZE_V1

GET_CH_T = 0x24
GET_CH_BIN_FMT = "<B12sHBbB"   # 설명: B( t ) 12s( uid ) H( msg_id ) B(ok) b(err) B(ch)
GET_CH_BIN_SIZE = struct.calcsize(GET_CH_BIN_FMT)

NODE_INFO_T = 0x40
NODE_INFO_HDR_FMT = "<B12sHBbH"   # t, uid, msg_id(u16), ok, err(i8), text_len(u16)
NODE_INFO_HDR_SIZE = struct.calcsize(NODE_INFO_HDR_FMT)
NODE_INFO_TEXT_MAX = 240

T_NODEINFO_BIN = 0x14
NODEINFO_FMT = "<B12sHBBHBBBBBB8sHH"   # Little-endian, packed
NODEINFO_SIZE = struct.calcsize(NODEINFO_FMT)

ENV_KEYS = ("temp","humi","pm1","pm25","pm10","co2","voc","ch2o","co","o3","no2","ax","ay","az")
def read_env_latest(path="/home/pi/config/env_latest.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
def read_gw_info_latest(path="/home/pi/gw_info.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
def read_gw_runtime_config(path="config/gw_runtime_config.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
def write_gw_runtime_config(data: dict, path="config/gw_runtime_config.json"):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False
def _strip_at_nul(b: bytes) -> bytes:
        """payload 안에 0x00이 섞여오면 그 지점에서 텍스트 종료로 간주"""
        i = b.find(b"\x00")
        return b if i < 0 else b[:i]

def _clean_text_bytes(b: bytes) -> bytes:
    # NUL 제거 + \r 유지
    b = _strip_at_nul(b)
    i = b.find(b"\x03")
    if i != -1:
        b = b[:i]
    return b

def _extract_env_values(latest: dict) -> dict:
    env_values = {}
    if isinstance(latest, dict):
        for k in ENV_KEYS:
            if k in latest:
                env_values[k] = latest[k]
        if "ts" in latest:
            env_values["ts"] = latest["ts"]
    return env_values

def _extract_gps_values(latest: dict) -> dict:
    if not isinstance(latest, dict):
        return {}
    gps = latest.get("gps")
    if not isinstance(gps, dict):
        return {}

    gps_values = {}
    for k in ("lat", "lon", "ts"):
        if gps.get(k) is not None:
            gps_values[k] = gps[k]
    return gps_values

def _looks_like_text_payload(b: bytes) -> bool:
    if not b:
        return False
    b2 = _clean_text_bytes(b)
    if len(b2) < 8:
        return False
    allowed = set(bytes(string.printable, "ascii")) | {0x0A, 0x0D, 0x09}
    ok = sum(1 for x in b2 if x in allowed)
    ratio = ok / len(b2)
    has_kv = (b"=" in b2) and (b"\n" in b2 or b"\r" in b2)
    return ratio >= 0.85 and has_kv

def _parse_kv_text(b: bytes) -> dict:
    b2 = _clean_text_bytes(b)
    text = b2.decode("ascii", errors="replace")
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
    kv = {}
    for ln in lines:
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv[k.strip()] = v.strip()
    return {"text": text, "lines": lines, "kv": kv}     

def parse_nodeinfo_bin(b: bytes):
    if len(b) < NODEINFO_SIZE:
        return None
    (t, uid, msg_id, ok, gid, mid,
     dev, dsp, rch0, rch1, txp, mode,
     mac8, fw_major, fw_minor) = struct.unpack_from(NODEINFO_FMT, b, 0)

    return {
        "t": t,
        "uid": uid.hex(),
        "msg_id": msg_id,
        "ok": int(ok),
        "gid": int(gid),
        "mid": int(mid),
        "dev": int(dev),
        "dsp": int(dsp),
        "rch": [int(rch0), int(rch1)],
        "txp": int(txp),
        "mode": int(mode),
        "mac": mac8.hex(),
        "fw": {"major": int(fw_major), "minor": int(fw_minor)},
    }

def _is_plausible_snap(parsed: dict) -> bool:
    try:
        fft_count = int(parsed.get("fft_count", 0) or 0)
        ok = int(parsed.get("ok"))
        err_code = int(parsed.get("err_code"))
    except Exception:
        return False

    if fft_count < 0 or fft_count > SNAP_FFT_PAIRS:
        return False
    if ok not in (0, 1):
        return False
    if err_code < -32 or err_code > 32:
        return False

    for idx in range(min(fft_count, len(parsed.get("fft_pairs", [])))):
        freq, amp = parsed["fft_pairs"][idx]
        if not isinstance(freq, (int, float)):
            return False
        if not (0.0 <= float(freq) <= 1_000_000.0):
            return False
        if amp is not None and (not isinstance(amp, (int, float)) or not (-1_000_000.0 <= float(amp) <= 1_000_000.0)):
            return False

    return True

def unpack_snap_bin(b: bytes):
    if len(b) < SNAP_BIN_SIZE or b[0] != T_SNAP:
        return None

    try:
        (
            t_val,
            ttl,
            uid_bytes,
            volt,
            curr,
            temp,
            light_on,
            f1_x100, a1_x1000,
            snap_count,
            ai_mse_x1000000,
            flags,
        ) = struct.unpack(SNAP_BIN_FMT, b[:SNAP_BIN_SIZE])
    except struct.error:
        return None

    ai_valid = int(flags) & 0x01
    ai_pred = (int(flags) >> 1) & 0x01
    ok = (int(flags) >> 2) & 0x01
    err_code = 0 if ok else 1

    base = {
        "t": t_val,
        "ttl": int(ttl),
        "uid_bytes": uid_bytes,
        "volt": round(float(volt), 4),
        "curr": round(float(curr), 4),
        "temp": round(float(temp), 4),
        "light_on": int(light_on),
        "fft_count": 1,
        "snap_count": int(snap_count),
        "msg_id32": int(snap_count),
        "ok": int(ok),
        "err_code": int(err_code),
        "ai_valid": int(ai_valid),
        "ai_mse": float(ai_mse_x1000000) / SNAP_AI_MSE_SCALE,
        "ai_pred": int(ai_pred),
        "flags": int(flags),
        "ai_raw": {
            "mse_x1000000": int(ai_mse_x1000000),
        },
        "tail_valid": True,
    }

    scaled = dict(base)
    scaled.update({
        "fft_pairs": [
            (float(f1_x100) / 100.0, float(a1_x1000) / 1000.0),
        ],
        "layout": "snap_40b_compact_scaled_fft_ai",
        "fft_raw": {
            "f1_x100": int(f1_x100),
            "a1_x1000": int(a1_x1000),
        },
    })
    if _is_plausible_snap(scaled):
        return scaled

    return None

def unpack_status_bin(b: bytes):
    if len(b) < STATUS_BIN_SIZE_V1 or b[0] != STATUS_T:
        return None

    if len(b) >= STATUS_BIN_SIZE_V2:
        try:
            (
                t_val,
                uid_bytes,
                volt,
                curr,
                temp,
                light_on,
                snap_count,
                msg_id32,
                ok,
                err_code,
            ) = struct.unpack(STATUS_BIN_FMT_V2, b[:STATUS_BIN_SIZE_V2])
            return {
                "t": t_val,
                "uid_bytes": uid_bytes,
                "volt": volt,
                "curr": curr,
                "temp": temp,
                "light_on": int(light_on),
                "snap_count": int(snap_count),
                "msg_id32": int(msg_id32),
                "ok": int(ok),
                "err_code": int(err_code),
            }
        except struct.error:
            pass

    (
        t_val,
        uid_bytes,
        volt,
        curr,
        temp,
        light_on,
        msg_id32,
        ok,
        err_code,
    ) = struct.unpack(STATUS_BIN_FMT_V1, b[:STATUS_BIN_SIZE_V1])
    return {
        "t": t_val,
        "uid_bytes": uid_bytes,
        "volt": volt,
        "curr": curr,
        "temp": temp,
        "light_on": int(light_on),
        "snap_count": None,
        "msg_id32": int(msg_id32),
        "ok": int(ok),
        "err_code": int(err_code),
    }

def _plausible_light_event_number(value, low, high):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) and low <= num <= high else None

def _plausible_light_event_rtc(parts):
    if not parts:
        return False
    year, month, day, hour, minute, sec, synced = parts
    return (
        2020 <= int(year) <= 2100
        and 1 <= int(month) <= 12
        and 1 <= int(day) <= 31
        and 0 <= int(hour) <= 23
        and 0 <= int(minute) <= 59
        and 0 <= int(sec) <= 59
        and int(synced) in (0, 1)
    )

def _read_light_event_rtc_at(b: bytes, off: int):
    if off + LIGHT_STATE_EVENT_RTC_EXT_SIZE <= len(b):
        parts = struct.unpack_from(LIGHT_STATE_EVENT_RTC_EXT_FMT, b, off)
        if _plausible_light_event_rtc(parts):
            return tuple(int(v) for v in parts), LIGHT_STATE_EVENT_RTC_EXT_SIZE, "u16_year"

    # Some firmware variants have used a one-byte year offset from 2019.
    if off + 7 <= len(b):
        yy, month, day, hour, minute, sec, synced = struct.unpack_from("<BBBBBBB", b, off)
        parts = (2019 + int(yy), int(month), int(day), int(hour), int(minute), int(sec), int(synced))
        if _plausible_light_event_rtc(parts):
            return parts, 7, "u8_year_offset_2019"

    return None

def _light_event_measure_score(temp, fft_count, fft_pairs, rtc_parts, valid_flags):
    score = 0
    valid_temp = bool(valid_flags & 0x04)
    valid_fft = bool(valid_flags & 0x08)
    valid_rtc = bool(valid_flags & 0x10)

    temp_ok = _plausible_light_event_number(temp, -40.0, 125.0) is not None
    if temp_ok:
        score += 3
        if valid_temp and abs(float(temp)) >= NODE_ZERO_EPS:
            score += 2
    elif valid_temp:
        score -= 5

    try:
        count = int(fft_count)
    except (TypeError, ValueError):
        return -100
    if 0 <= count <= SNAP_FFT_PAIRS:
        score += 1
    else:
        return -100

    good_pairs = 0
    for freq, amp in fft_pairs[:count]:
        if (
            _plausible_light_event_number(freq, 0.0, 1_000_000.0) is not None
            and _plausible_light_event_number(amp, -1_000_000.0, 1_000_000.0) is not None
        ):
            good_pairs += 1
    if count == good_pairs:
        score += good_pairs * 3
    else:
        score -= 5
    if valid_fft and count > 0:
        score += 2
    elif valid_fft and count == 0:
        score -= 2

    if rtc_parts and _plausible_light_event_rtc(rtc_parts):
        score += 4
    elif valid_rtc:
        score -= 3

    return score

def _resync_light_event_measurements(b: bytes, valid_flags: int, current: dict):
    head_size = struct.calcsize("<B12sIBBBBIff")
    best = None
    for shift in range(-4, 9):
        temp_off = head_size + shift
        if temp_off < head_size or temp_off + 5 > len(b):
            continue
        fft_base = temp_off + 5
        if fft_base + (SNAP_FFT_PAIRS * 8) > len(b):
            continue
        try:
            temp = struct.unpack_from("<f", b, temp_off)[0]
            fft_count_raw = int(b[temp_off + 4])
            fft_count = max(0, min(SNAP_FFT_PAIRS, fft_count_raw))
            f1, a1, f2, a2 = struct.unpack_from("<ffff", b, fft_base)
        except struct.error:
            continue

        rtc_off = fft_base + (SNAP_FFT_PAIRS * 8)
        rtc = _read_light_event_rtc_at(b, rtc_off)
        rtc_parts = rtc[0] if rtc else None
        pairs = [(float(f1), float(a1)), (float(f2), float(a2))]
        score = _light_event_measure_score(temp, fft_count_raw, pairs, rtc_parts, valid_flags)
        candidate = {
            "score": score,
            "shift": shift,
            "temp": float(temp),
            "fft_count": fft_count,
            "fft_count_raw": fft_count_raw,
            "fft_pairs": pairs[:fft_count],
            "rtc": rtc,
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    if best is None or best["score"] <= current.get("score", -100):
        return None
    return best

def unpack_light_state_event_bin(b: bytes):
    if len(b) < LIGHT_STATE_EVENT_BIN_SIZE or b[0] != T_LIGHT_STATE_EVENT:
        return None

    (
        t_val,
        uid_bytes,
        event_id,
        valid_flags,
        light_on,
        mode,
        reason,
        tick_ms,
        voltage,
        current,
        temp,
        fft_count,
        f1, a1,
        f2, a2,
    ) = struct.unpack(LIGHT_STATE_EVENT_BIN_FMT, b[:LIGHT_STATE_EVENT_BIN_SIZE])

    valid_flags = int(valid_flags) & 0xFF
    fft_count = max(0, min(SNAP_FFT_PAIRS, int(fft_count)))
    fft_pairs = [(float(f1), float(a1)), (float(f2), float(a2))]

    result = {
        "t": int(t_val),
        "uid_bytes": uid_bytes,
        "event_id": int(event_id),
        "valid_flags": valid_flags,
        "light_on": int(light_on) & 0x01,
        "mode": int(mode),
        "reason": int(reason),
        "tick_ms": int(tick_ms),
        "voltage": float(voltage),
        "current": float(current),
        "temp": float(temp),
        "fft_count": fft_count,
        "fft_pairs": fft_pairs[:fft_count],
        "parse_layout": "normal",
    }

    rtc = _read_light_event_rtc_at(b, LIGHT_STATE_EVENT_BIN_SIZE)
    if rtc:
        (rtc_year, rtc_month, rtc_day, rtc_hour, rtc_min, rtc_sec, rtc_synced), _, rtc_layout = rtc
        result.update({
            "rtc_year": int(rtc_year),
            "rtc_month": int(rtc_month),
            "rtc_day": int(rtc_day),
            "rtc_hour": int(rtc_hour),
            "rtc_min": int(rtc_min),
            "rtc_sec": int(rtc_sec),
            "rtc_synced": int(rtc_synced) & 0x01,
            "rtc_layout": rtc_layout,
            "rtc": (
                f"{int(rtc_year):04d}-{int(rtc_month):02d}-{int(rtc_day):02d} "
                f"{int(rtc_hour):02d}:{int(rtc_min):02d}:{int(rtc_sec):02d}"
            ),
        })

    current_score = _light_event_measure_score(
        result["temp"],
        result["fft_count"],
        fft_pairs,
        (
            result["rtc_year"],
            result["rtc_month"],
            result["rtc_day"],
            result["rtc_hour"],
            result["rtc_min"],
            result["rtc_sec"],
            result["rtc_synced"],
        ) if "rtc_year" in result else None,
        valid_flags,
    )
    alt = _resync_light_event_measurements(b, valid_flags, {"score": current_score})
    if alt is not None:
        result.update({
            "temp": alt["temp"],
            "fft_count": alt["fft_count"],
            "fft_pairs": alt["fft_pairs"],
            "parse_layout": f"resync_shift_{alt['shift']}",
            "parse_score": alt["score"],
            "fft_count_raw": alt["fft_count_raw"],
        })
        if alt.get("rtc"):
            (rtc_year, rtc_month, rtc_day, rtc_hour, rtc_min, rtc_sec, rtc_synced), _, rtc_layout = alt["rtc"]
            result.update({
                "rtc_year": int(rtc_year),
                "rtc_month": int(rtc_month),
                "rtc_day": int(rtc_day),
                "rtc_hour": int(rtc_hour),
                "rtc_min": int(rtc_min),
                "rtc_sec": int(rtc_sec),
                "rtc_synced": int(rtc_synced) & 0x01,
                "rtc_layout": rtc_layout,
                "rtc": (
                    f"{int(rtc_year):04d}-{int(rtc_month):02d}-{int(rtc_day):02d} "
                    f"{int(rtc_hour):02d}:{int(rtc_min):02d}:{int(rtc_sec):02d}"
                ),
            })
    else:
        result["parse_score"] = current_score

    return result
     
def cut_cfg_text(payload: bytes) -> bytes:
    # 1) 가장 확실한 종료: ":<ETX>"
    m = payload.find(b":\x03")
    if m != -1:
        return payload[:m]  # ':' 이전까지만

    # 2) 혹시 ETX만 오는 경우도 대비
    m = payload.find(b"\x03")
    if m != -1:
        return payload[:m]

    # 3) 마지막 안전장치: 비 printable 나오면 거기서 컷
    out = bytearray()
    for x in payload:
        if x in (0x0A, 0x0D, 0x09):   # \n \r \t 허용
            out.append(x)
            continue
        if 0x20 <= x <= 0x7E:        # printable ASCII
            out.append(x)
            continue
        break
    return bytes(out)  
NODE_CMD_MAP = {
  "get_node_uid":        {"code": CMD_GET_UID,  "need": []},
  "get_channel":         {"code": CMD_GET_CH,   "need": []},
  "set_channel":         {"code": CMD_SET_CH,   "need": ["ch"]},          # 서버 payload 필드명에 맞추기
  "get_fft_data":        {"code": CMD_GET_FFT,  "need": []},
  
  "get_voltage_current": {"code": CMD_GET_VI,   "need": []},
  "get_node_info":       {"code": CMD_GET_INFO, "need": []},
  "set_power_ctrl":      {"code": CMD_SET_POWER, "need": ["status"]},       # 예: 0/1
  "set_node_info":     {"code": CMD_SET_NODE_INFO,     "need": ["info"]},
  "set_time_interval": {"code": CMD_SET_TIME_INTERVAL, "need": ["period_min"]},
}

WISUN_STATUS_OK = 0
WISUN_STATUS_OFFLINE = 1
WISUN_STATUS_DEGRADED = 2

WISUN_STATUS_TEXT = {
    WISUN_STATUS_OK: "ok",
    WISUN_STATUS_OFFLINE: "offline",
    WISUN_STATUS_DEGRADED: "degraded",
}

NODE_VALUE_LIMITS = {
    "voltage": (0.0, 100.0),
    "current": (0.0, 50.0),
    "temperature": (-40.0, 125.0),
    "fft_freq": (0.0, 1_000_000.0),
    "fft_amp": (-1_000_000.0, 1_000_000.0),
}
NODE_ZERO_EPS = 1e-9
FFT_AMP_MISSING_EPS = 1e-6


def _finite_number(value):
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _bounded_number(value, name: str):
    num = _finite_number(value)
    if num is None:
        return None
    if abs(num) < NODE_ZERO_EPS:
        num = 0.0
    low, high = NODE_VALUE_LIMITS[name]
    if not (low <= num <= high):
        return None
    return num


def _sanitize_light_on(value):
    if value is None:
        return None
    try:
        light = int(value)
    except (TypeError, ValueError):
        return None
    return light if light in (0, 1) else None


def _sanitize_fft(fft):
    if not fft:
        return None

    clean = []
    for pair in fft:
        if not isinstance(pair, (list, tuple)) or len(pair) < 1:
            continue
        freq = _bounded_number(pair[0], "fft_freq")
        if freq is None:
            continue
        amp = None
        if len(pair) > 1:
            amp_raw = _finite_number(pair[1])
            if amp_raw is not None and abs(amp_raw) >= FFT_AMP_MISSING_EPS:
                amp = _bounded_number(amp_raw, "fft_amp")
        clean.append([freq, amp])
        if len(clean) >= SNAP_FFT_PAIRS:
            break

    return clean or None

def _fft_missing_value(fft):
    clean = _sanitize_fft(fft)
    if not clean:
        return True
    for pair in clean:
        if len(pair) < 2 or pair[0] is None or pair[1] is None:
            return True
    return False

def _complete_fft_or_none(fft):
    clean = _sanitize_fft(fft)
    if _fft_missing_value(clean):
        return None
    return clean

def _merge_fft_missing_values(current_fft, fallback_fft):
    current = _sanitize_fft(current_fft)
    fallback = _complete_fft_or_none(fallback_fft)
    if not current:
        return fallback
    if not fallback:
        return current

    merged = []
    for idx, pair in enumerate(current):
        freq = pair[0] if len(pair) > 0 else None
        amp = pair[1] if len(pair) > 1 else None
        if idx < len(fallback):
            fb_freq, fb_amp = fallback[idx]
            if freq is None:
                freq = fb_freq
            if amp is None:
                amp = fb_amp
        merged.append([freq, amp])
    return _sanitize_fft(merged)


def _sanitize_node_measurements(*, voltage=None, current=None, temperature=None, light_on=None, fft=None):
    return {
        "voltage": _bounded_number(voltage, "voltage"),
        "current": _bounded_number(current, "current"),
        "temperature": _bounded_number(temperature, "temperature"),
        "light_on": _sanitize_light_on(light_on),
        "fft": _sanitize_fft(fft),
    }

def _sanitize_ai_result(*, ai_valid=None, ai_mse=None, ai_pred=None):
    clean_valid = None
    if ai_valid is not None:
        try:
            valid_int = int(ai_valid)
            if valid_int in (0, 1):
                clean_valid = valid_int
        except (TypeError, ValueError):
            clean_valid = None

    clean_mse = None
    if ai_mse is not None:
        try:
            mse_float = float(ai_mse)
            if math.isfinite(mse_float) and 0.0 <= mse_float <= 1_000_000.0:
                clean_mse = mse_float
        except (TypeError, ValueError):
            clean_mse = None

    clean_pred = None
    if ai_pred is not None:
        try:
            pred_int = int(ai_pred)
            if -128 <= pred_int <= 127:
                clean_pred = pred_int
        except (TypeError, ValueError):
            clean_pred = None

    return {
        "ai_valid": clean_valid,
        "ai_mse": clean_mse,
        "ai_pred": clean_pred,
    }


def _fallback_snap_head_usable(snap: dict) -> bool:
    if snap.get("tail_valid"):
        return True
    try:
        fft_count = int(snap.get("fft_count", 0) or 0)
    except (TypeError, ValueError):
        return False
    return (
        _bounded_number(snap.get("volt"), "voltage") is not None
        and _bounded_number(snap.get("curr"), "current") is not None
        and _bounded_number(snap.get("temp"), "temperature") is not None
        and _sanitize_light_on(snap.get("light_on")) is not None
        and 0 <= fft_count <= SNAP_FFT_PAIRS
    )


def build_rtc_kst_payload_from_dt(when: datetime.datetime) -> bytes:
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now = when.astimezone(kst)
    year = int(now.year)
    return bytes([
        (year >> 8) & 0xFF,
        year & 0xFF,
        now.month & 0xFF,
        now.day & 0xFF,
        now.hour & 0xFF,
        now.minute & 0xFF,
        now.second & 0xFF,
    ])

class CmdRouter:
    def __init__(self, wisun, mqtt, reg, attach_raw_bytes: bool = False,  gwid: str = "gw001", store: NodeStore | None = None, scheduler = None,):
        self.wisun = wisun
        self.mqtt = mqtt
        self.reg  = reg
        self.attach_raw_bytes = attach_raw_bytes  
        self.gwid = gwid
        self.store = store
        self.scheduler = scheduler
        self.runtime_config_path = "config/gw_runtime_config.json"

        self.snap_batch_period_sec = 60  # gateway snap batch publish period (seconds)
        self.gw_env_period_sec = 60.0
        self._load_runtime_config()
        self._snap_cycle_seen_mids = set()
        self._snap_cycle_lock = threading.Lock()
        self._snap_cycle_start_ts = time.time()

        self.pending_status = {}   
        self.next_mid = 1
        self._snap_frag_store = {}
        self.pending_multi = {}
        self.comm_health = {}
        self._last_wisun_status = {}
        self._last_good_measurements = {}
        self.uplink_dedupe_window_sec = 2.0
        self._recent_uplink = {}
        self._recent_uplink_lock = threading.Lock()
        self._comm_lock = threading.Lock()
        self._server_time_base = None
        self._server_time_monotonic = None
        self.cache = {
            "snap": {},    
            "status": {},  
            "uid": {},   
        }
        self.pending = defaultdict(deque)
        self.log = logging.getLogger("gw")

        self._stop_event = threading.Event()

        t = threading.Thread(target=self._snap_batch_loop, daemon=True)
        t.start()

        self._pending_watchdog = threading.Thread(
            target=self._pending_watchdog_loop,
            daemon=True
        )
        self._pending_watchdog.start()
        
    
        self._env_thread = threading.Thread(
            target=self._publish_gw_periodic_env,
            daemon=True
        )
        self._env_thread.start()
        _verbose_print(
            f"[GW_ENV] thread started gid={self.gwid} "
            f"period={float(getattr(self, 'gw_env_period_sec', 60.0) or 60.0)}s"
        )
        if self.wisun is not None and hasattr(self.wisun, "add_frame_error_handler"):
            try:
                self.wisun.add_frame_error_handler(self.on_wisun_frame_error)
            except Exception as e:
                self.log.warning("failed to register wisun frame error handler: %r", e)
        self._sync_registry_online_window()

    def _snap_online_window_detail(self, mid: int | None = None) -> tuple[int, str, int | None]:
        if mid is not None and self.store is not None:
            try:
                rec = self._store_latest_by_mid(int(mid))
            except Exception:
                rec = None
            if isinstance(rec, dict):
                try:
                    node_interval = int(rec.get("interval") or 0)
                except Exception:
                    node_interval = 0
                if node_interval > 0:
                    return (
                        int(node_interval) + int(self.snap_batch_period_sec),
                        "node_interval",
                        int(node_interval),
                    )

        period_sec = None
        if self.scheduler is not None and hasattr(self.scheduler, "get_snap_period_sec"):
            try:
                period_sec = self.scheduler.get_snap_period_sec()
            except Exception:
                period_sec = None

        if period_sec is None or period_sec <= 0:
            return 300, "default", None

        return int(period_sec) + int(self.snap_batch_period_sec), "scheduler", int(period_sec)

    def _snap_online_window_sec(self, mid: int | None = None) -> int:
        window_sec, _source, _base = self._snap_online_window_detail(mid=mid)
        return window_sec

    def _sync_registry_online_window(self) -> int:
        online_window_sec = self._snap_online_window_sec()
        if self.reg is not None and hasattr(self.reg, "ttl"):
            try:
                self.reg.ttl = int(online_window_sec)
            except Exception:
                pass
        return online_window_sec

    def _load_runtime_config(self):
        cfg = read_gw_runtime_config(self.runtime_config_path)
        try:
            snap_batch = float(cfg.get("snap_batch_period_sec", self.snap_batch_period_sec) or self.snap_batch_period_sec)
        except Exception:
            snap_batch = float(self.snap_batch_period_sec)
        try:
            gw_env = float(cfg.get("gw_env_period_sec", self.gw_env_period_sec) or self.gw_env_period_sec)
        except Exception:
            gw_env = float(self.gw_env_period_sec)

        self.snap_batch_period_sec = max(1.0, min(3600.0, snap_batch))
        self.gw_env_period_sec = max(1.0, min(3600.0, gw_env))

    def _save_runtime_config(self):
        return write_gw_runtime_config({
            "snap_batch_period_sec": float(self.snap_batch_period_sec),
            "gw_env_period_sec": float(self.gw_env_period_sec),
        }, self.runtime_config_path)
        
    
    def _publish_gw_periodic_env(self):
        topic = f"gw/{self.gwid}/gw_env"

        while not self._stop_event.is_set():
            wait_sec = float(getattr(self, "gw_env_period_sec", 60.0) or 60.0)
            if wait_sec <= 0:
                wait_sec = 1.0

            _verbose_print(f"[GW_ENV] tick gid={self.gwid} period={wait_sec}s")
            latest = read_env_latest()
            env_values = _extract_env_values(latest)
            gps_latest = read_gw_info_latest()
            gps_values = _extract_gps_values(gps_latest)

            if env_values or gps_values:
                msg = {
                    "cmd": "gw_env",          # ← 이벤트/스냅샷용 cmd
                    "gid": self.gwid,
                    "reason": "periodic",
                    "ts": int(time.time()),
                    "interval": float(getattr(self, "snap_batch_period_sec", 60.0) or 60.0),
                    "sensor_interval": float(getattr(self, "gw_env_period_sec", 60.0) or 60.0),
                }
                if env_values:
                    msg["values"] = env_values
                if gps_values:
                    msg["gps"] = gps_values
                self.mqtt.publish_json(topic, msg)
                _verbose_print(f"[GW_ENV] publish topic={topic} has_env={bool(env_values)} has_gps={bool(gps_values)}")
                _verbose_print("[PUB] gw/env:", msg)
            else:
                env_ts = env_values.get("ts") if isinstance(env_values, dict) else None
                gps_ts = gps_values.get("ts") if isinstance(gps_values, dict) else None
                _verbose_print(
                    f"[GW_ENV] skip topic={topic} has_env={bool(env_values)} "
                    f"has_gps={bool(gps_values)} env_ts={env_ts} gps_ts={gps_ts}"
                )

            self._stop_event.wait(wait_sec)

    def stop(self):
        if hasattr(self, "_stop_event"):
            self._stop_event.set()

    def on_server_cmd(self, payload: dict, topic: str):
        _verbose_print(f"[MQTT RX CMD] topic={topic} payload={payload}")        
        root = gw_id = topic_cmd = None
        try:
            if not isinstance(payload, dict):
                print(f"[CMD_ROUTER] ignore non-dict payload topic={topic}")
                return
            cmd = payload.get("cmd")
            target = payload.get("target", "node")
            data = payload.get("payload") or payload.get("data") or {}
            parts = topic.split("/")
            root = parts[0] if len(parts) > 0 else ""
            gw_id = parts[1] if len(parts) > 1 else None
            raw_msg_id = payload.get("msg_id")
            try:
                msg_id_int = int(raw_msg_id) if raw_msg_id is not None else (int(time.time() * 1000) & 0xFFFFFFFF)
            except (TypeError, ValueError):
                msg_id_int = int(time.time() * 1000) & 0xFFFFFFFF

            if raw_msg_id is None:
                raw_msg_id = msg_id_int
            topic_cmd = parts[2] if len(parts) > 2 else None

            if root == "node" and gw_id == self.gwid and topic_cmd == "response":
                return

            if root == "node" and gw_id == self.gwid and topic_cmd == "ctrl" and len(parts) > 3:
                if not cmd:
                    cmd = parts[3]

            if root == "gw" and gw_id == self.gwid and topic_cmd in ("gw_env", "mid_lists", "boot_success"):
                return

            # topic이 gw/{gwid}/{cmd} 형태면 target을 gw로 보정
            if root == "gw" and gw_id == self.gwid and topic_cmd:
                if topic_cmd not in ("cmd_result", "response", "raw", "gw_env", "mid_lists", "boot_success"):
                    if not target or target == "node":
                        target = "gw"
                    if not cmd:
                        cmd = topic_cmd
        except Exception as e:
            print(f"[CMD_ROUTER] exception: {e} topic={topic} root={root} gw_id={gw_id} topic_cmd={topic_cmd} payload={payload}")
            return
        # -----------------------------
        # 공통 헬퍼
        # -----------------------------
        def _publish_gw(resp: dict):
            self.mqtt.publish_json(f"gw/{self.gwid}/cmd_result", resp)

        node_topic = lambda c: f"node/{self.gwid}/response/{c}"

        def _publish_node(cmd_key: str, resp: dict):
            self.mqtt.publish_json(node_topic(cmd_key), resp)

        def _base_cmd(c: str) -> str:
            return c[:-4] if c.endswith("_ack") else c

        def _node_fail(cmd_key: str, reason: str, mid: int | None = None):
            base = _base_cmd(cmd_key)
            resp = {
                "cmd": f"{base}_ack",
                "gid": self.gwid,
                "msg_id": raw_msg_id,
                "result": "fail",
                "reason": reason,
            }
            if mid is not None:
                resp["mid"] = mid
                resp.update(self._wisun_status_info(mid))
            _publish_node(base, resp)   
            print(f"[CMD] {base} fail:", resp)

        # =========================================================================
        # Gateway commands (gw/* 또는 target=gw)
        # =========================================================================
        if target in ("gw", "gateway"):
            GW_ALIAS = {"change_gw_id": "set_gw_id"}
            gw_cmd = GW_ALIAS.get(cmd, cmd)

            def _gw_set_gw_id():
                new_gwid = (
                    data.get("id")
                    or data.get("new_gwid")
                    or data.get("gwid")
                    or data.get("gid")
                )

                if not new_gwid:
                    resp = {
                        "cmd": "set_gw_id_ack",
                        "old_gwid": self.gwid,
                        "new_gwid": None,
                        "result": "fail",
                        "reason": "missing_id",
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    _verbose_print("[CMD] set_gw_id_ack:", resp)
                    return

                save_ok = False
                try:
                    os.makedirs("/home/pi/config", exist_ok=True)
                    with open("/home/pi/config/gw_id.conf", "w", encoding="utf-8") as f:
                        f.write(str(new_gwid).strip() + "\n")
                    save_ok = True
                except Exception as e:
                    print("[CMD] set_gw_id: save failed:", e)

                resp = {
                    "cmd": "set_gw_id_ack",
                    "old_gwid": self.gwid,
                    "new_gwid": new_gwid,
                    "result": "success" if save_ok else "fail",
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                _verbose_print("[CMD] set_gw_id_ack:", resp)

                if save_ok:
                    print("[CMD] set_gw_id: rebooting to apply new gwid:", new_gwid)

                    def _reboot_later():
                        time.sleep(2)
                        os.system("sudo reboot")

                    threading.Thread(target=_reboot_later, daemon=True).start()

            def _gw_reboot():
                delay_sec = int(data.get("delay_sec", 1) or 1)
                resp = {
                    "cmd": "reboot_ack",
                    "gid": self.gwid,
                    "result": "success",
                    "delay_sec": delay_sec,
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                _verbose_print("[CMD] reboot_ack:", resp)

                def _reboot_later():
                    print(f"[CMD] reboot: rebooting in {delay_sec} sec...")
                    time.sleep(delay_sec)
                    os.system("sudo reboot")

                threading.Thread(target=_reboot_later, daemon=True).start()

            def _gw_get_gw_info():
                info = {
                    "gwid": self.gwid,
                    "uid": getattr(self, "hw_uid", None),
                    "fw": {"version": getattr(self, "fw_version", "unknown")},
                }
                resp = {"cmd": "get_gw_info_ack", "info": info, "msg_id": raw_msg_id}
                _publish_gw(resp)
                _verbose_print("[CMD] get_gw_info_ack:", resp)

            def _gw_get_wisun_status():
                # 1) 신규: AT+CFG? 텍스트 반환 helper
                if hasattr(self.wisun, "at_get_cfg"):
                    try:
                        cfg_text = self.wisun.at_get_cfg(timeout=2.0)
                        resp = {
                            "cmd": "get_wisun_status_ack",
                            "wisun": {"cfg": cfg_text},
                            "msg_id": raw_msg_id,
                        }
                        _publish_gw(resp)
                        _verbose_print("[CMD] get_wisun_status_ack:", resp)
                        return
                    except Exception as e:
                        resp = {
                            "cmd": "get_wisun_status_ack",
                            "wisun": {},
                            "result": "fail",
                            "reason": f"at_error:{e}",
                            "msg_id": raw_msg_id,
                        }
                        _publish_gw(resp)
                        print("[CMD] get_wisun_status_ack fail:", resp)
                        return

                # 2) 구버전 호환
                if hasattr(self.wisun, "get_status_dict"):
                    wisun_stat = self.wisun.get_status_dict()
                    resp = {
                        "cmd": "get_wisun_status_ack",
                        "wisun": wisun_stat,
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    _verbose_print("[CMD] get_wisun_status_ack:", resp)
                    return

                resp = {
                    "cmd": "get_wisun_status_ack",
                    "wisun": {},
                    "result": "fail",
                    "reason": "no_helper",
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                print("[CMD] get_wisun_status_ack fail:", resp)

            def _gw_get_env_info(raw_msg_id=None):
                latest = read_env_latest()
                env_values = _extract_env_values(latest)

                resp = {
                    "cmd": "get_env_info_ack",
                    "gid": self.gwid,          # gw 응답이면 gid 넣는 게 보통 더 좋음
                    "values": env_values,
                    "ok": bool(env_values),
                    "msg_id": raw_msg_id,
                    "ts": int(time.time()),
                }
                _publish_gw(resp)
                _verbose_print("[CMD] get_env_info_ack:", resp)

            def _gw_set_gw_time():
                time_str = (
                    data.get("time")
                    or data.get("datetime")
                    or data.get("value")
                    or data.get("timestamp")
                    or payload.get("time")
                    or payload.get("datetime")
                    or payload.get("value")
                    or payload.get("timestamp")
                )
                if not time_str:
                    resp = {
                        "cmd": "set_gw_time_ack",
                        "result": "fail",
                        "reason": "missing_time",
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    print("[CMD] set_gw_time_ack fail:", resp)
                    return

                try:
                    time_text = str(time_str).strip()
                    if time_text.endswith("Z"):
                        time_text = time_text[:-1]
                    if "T" in time_text:
                        time_text = time_text.replace("T", " ", 1)
                    if "." in time_text:
                        time_text = time_text.split(".", 1)[0]

                    parsed_time = datetime.datetime.strptime(time_text, "%Y-%m-%d %H:%M:%S")
                    server_time_kst = parsed_time.replace(
                        tzinfo=datetime.timezone(datetime.timedelta(hours=9))
                    )
                    normalized_time = parsed_time.strftime("%Y-%m-%d %H:%M:%S")
                    subprocess.run(["sudo", "date", "-s", normalized_time], check=True)
                    subprocess.run(["sudo", "hwclock", "-w"], check=False)
                    self._server_time_base = server_time_kst
                    self._server_time_monotonic = time.monotonic()
                    node_rtc_sync = {
                        "queued": False,
                        "target_mid": 0,
                    }
                    try:
                        if self.scheduler is not None and hasattr(self.scheduler, "sync_rtc_kst_broadcast"):
                            self.scheduler.sync_rtc_kst_broadcast(server_time_kst)
                        else:
                            rtc_extra = build_rtc_kst_payload_from_dt(server_time_kst)
                            rtc_msg_id16 = int(server_time_kst.timestamp()) & 0xFFFF
                            self.wisun.send_cmd_bytes(
                                0,
                                CMD_SET_RTC_KST,
                                msg_id=rtc_msg_id16,
                                flags=0x00,
                                extra=rtc_extra,
                            )
                        node_rtc_sync = {
                            "queued": True,
                            "target_mid": 0,
                            "time": server_time_kst.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        print(f"[CMD] set_gw_time -> rtc_sync_broadcast time={node_rtc_sync['time']}")
                    except Exception as e:
                        node_rtc_sync = {
                            "queued": False,
                            "target_mid": 0,
                            "error": str(e),
                        }
                        print(f"[CMD] set_gw_time rtc_sync_broadcast failed: {e}")

                    resp = {
                        "cmd": "set_gw_time_ack",
                        "result": "success",
                        "time": normalized_time,
                        "time_source": "server",
                        "node_rtc_sync": node_rtc_sync,
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    _verbose_print("[CMD] set_gw_time_ack:", resp)
                except Exception as e:
                    resp = {
                        "cmd": "set_gw_time_ack",
                        "result": "fail",
                        "reason": str(e),
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    print("[CMD] set_gw_time_ack fail:", resp)

            def _gw_send_ping():
                resp = {
                    "cmd": "send_ping_ack",
                    "gid": self.gwid,
                    "ts": int(time.time()),
                    "result": "success",
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                _verbose_print("[CMD] send_ping_ack:", resp)

            def _gw_set_env_interval():
                interval_raw = (
                    data.get("interval")
                    if data.get("interval") is not None
                    else data.get("period_sec")
                    if data.get("period_sec") is not None
                    else data.get("env_interval")
                    if data.get("env_interval") is not None
                    else payload.get("interval")
                    if payload.get("interval") is not None
                    else payload.get("period_sec")
                    if payload.get("period_sec") is not None
                    else payload.get("env_interval")
                    if payload.get("env_interval") is not None
                    else None
                )
                sensor_interval_raw = (
                    data.get("sensor_interval")
                    if data.get("sensor_interval") is not None
                    else data.get("gw_env_interval")
                    if data.get("gw_env_interval") is not None
                    else payload.get("sensor_interval")
                    if payload.get("sensor_interval") is not None
                    else payload.get("gw_env_interval")
                    if payload.get("gw_env_interval") is not None
                    else None
                )

                if interval_raw is None and sensor_interval_raw is None:
                    resp = {
                        "cmd": "set_env_interval_ack",
                        "gid": self.gwid,
                        "result": "fail",
                        "reason": "missing_interval",
                        "msg_id": raw_msg_id,
                        "ts": int(time.time()),
                    }
                    _publish_gw(resp)
                    print("[CMD] set_env_interval_ack fail:", resp)
                    return

                interval_sec = None
                sensor_interval_sec = None

                if interval_raw is not None:
                    try:
                        interval_sec = max(1.0, min(3600.0, float(interval_raw)))
                    except Exception:
                        resp = {
                            "cmd": "set_env_interval_ack",
                            "gid": self.gwid,
                            "result": "fail",
                            "reason": "bad_interval",
                            "msg_id": raw_msg_id,
                            "ts": int(time.time()),
                        }
                        _publish_gw(resp)
                        print("[CMD] set_env_interval_ack fail:", resp)
                        return
                    self.snap_batch_period_sec = interval_sec

                if sensor_interval_raw is not None:
                    try:
                        sensor_interval_sec = max(1.0, min(3600.0, float(sensor_interval_raw)))
                    except Exception:
                        resp = {
                            "cmd": "set_env_interval_ack",
                            "gid": self.gwid,
                            "result": "fail",
                            "reason": "bad_sensor_interval",
                            "msg_id": raw_msg_id,
                            "ts": int(time.time()),
                        }
                        _publish_gw(resp)
                        print("[CMD] set_env_interval_ack fail:", resp)
                        return
                    self.gw_env_period_sec = sensor_interval_sec

                online_window_sec = self._sync_registry_online_window()
                saved_ok = self._save_runtime_config()

                resp = {
                    "cmd": "set_env_interval_ack",
                    "gid": self.gwid,
                    "result": "success",
                    "msg_id": raw_msg_id,
                    "ts": int(time.time()),
                    "interval_sec": interval_sec,
                    "sensor_interval_sec": sensor_interval_sec,
                    "snap_batch_period_sec": self.snap_batch_period_sec,
                    "gw_env_period_sec": self.gw_env_period_sec,
                    "online_window_sec": online_window_sec,
                    "saved": bool(saved_ok),
                }
                _publish_gw(resp)
                _verbose_print("[CMD] set_env_interval_ack:", resp)

            def _gw_set_gw_env_interval():
                interval_raw = (
                    data.get("interval")
                    if data.get("interval") is not None
                    else data.get("period_sec")
                    if data.get("period_sec") is not None
                    else data.get("gw_env_interval")
                    if data.get("gw_env_interval") is not None
                    else payload.get("interval")
                    if payload.get("interval") is not None
                    else payload.get("period_sec")
                    if payload.get("period_sec") is not None
                    else payload.get("gw_env_interval")
                    if payload.get("gw_env_interval") is not None
                    else None
                )
                if interval_raw is None:
                    resp = {
                        "cmd": "set_gw_env_interval_ack",
                        "gid": self.gwid,
                        "result": "fail",
                        "reason": "missing_interval",
                        "msg_id": raw_msg_id,
                        "ts": int(time.time()),
                    }
                    _publish_gw(resp)
                    print("[CMD] set_gw_env_interval_ack fail:", resp)
                    return

                try:
                    interval_sec = float(interval_raw)
                except Exception:
                    resp = {
                        "cmd": "set_gw_env_interval_ack",
                        "gid": self.gwid,
                        "result": "fail",
                        "reason": "bad_interval",
                        "msg_id": raw_msg_id,
                        "ts": int(time.time()),
                    }
                    _publish_gw(resp)
                    print("[CMD] set_gw_env_interval_ack fail:", resp)
                    return

                interval_sec = max(1.0, min(3600.0, interval_sec))
                self.gw_env_period_sec = interval_sec
                saved_ok = self._save_runtime_config()

                resp = {
                    "cmd": "set_gw_env_interval_ack",
                    "gid": self.gwid,
                    "result": "success",
                    "msg_id": raw_msg_id,
                    "ts": int(time.time()),
                    "interval_sec": interval_sec,
                    "gw_env_period_sec": self.gw_env_period_sec,
                    "saved": bool(saved_ok),
                }
                _publish_gw(resp)
                _verbose_print("[CMD] set_gw_env_interval_ack:", resp)

            gw_handlers = {
                "set_gw_id": _gw_set_gw_id,
                "reboot": _gw_reboot,
                "get_gw_info": _gw_get_gw_info,
                "get_wisun_status": _gw_get_wisun_status,
                "get_env_info": _gw_get_env_info,
                "set_env_interval": _gw_set_env_interval,
                "set_gw_env_interval": _gw_set_gw_env_interval,
                "set_gw_time": _gw_set_gw_time,
                "send_ping": _gw_send_ping,
            }

            h = gw_handlers.get(gw_cmd)
            if not h:
                resp = {
                    "cmd": gw_cmd,
                    "result": "fail",
                    "reason": "unknown_gw_cmd",
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                print("[CMD] unknown gw cmd:", gw_cmd)
                return

            h()
            return

        # =========================================================================
        # Node commands (node/* 또는 기본 target=node)
        # =========================================================================
        NODE_ALIAS = {"get_status": "get_node_status"}
        node_cmd = NODE_ALIAS.get(cmd, cmd)

        """ NODE_FORWARD_TABLE: dict[str, dict] = {
            "get_fft_data":         {"cmd_code": None, "need": ["mid"]},
            "get_voltage_current":  {"cmd_code": None, "need": ["mid"]},
            "get_node_uid":         {"cmd_code": None, "need": ["mid"]},
            "get_channel":          {"cmd_code": None, "need": ["mid"]},
            "get_node_info":        {"cmd_code": None, "need": ["mid"]},
            "set_power_ctrl":       {"cmd_code": None, "need": ["mid"]},
            "set_channel":          {"cmd_code": None, "need": ["mid", "ch"]},
            "set_node_info":        {"cmd_code": None, "need": ["mid"]},
            "set_time_interval":    {"cmd_code": None, "need": ["mid", "period_min"]},
        } """

        def _node_forward_simple(cmd_key: str):
            spec = NODE_CMD_MAP.get(cmd_key)
            if not spec:
                _node_fail(cmd_key, "unknown_cmd")
                return
            data = payload.get("data") or {}
            pdata = payload.get("payload") or {}
            # mid는 서버 포맷에 따라 payload 최상위 / data 안쪽 둘 다 대비
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail(cmd_key, "missing_mid")
                return

            # need 필드 검사
            for k in spec.get("need", []):
                if (k not in data) and (k not in pdata):
                    _node_fail(cmd_key, f"missing_{k}", mid=mid)
                    return

            cmd_code = int(spec["code"])

            extra = b""
            # extra 패킹(필요한 것만 최소로)
            if cmd_key == "set_channel":
                ch = int(data.get("ch", payload.get("ch", 0)) or 0) & 0xFF
                extra = bytes([ch])

            elif cmd_key == "set_power_ctrl":
                # data/pdata는 위에서 이미 만들어졌지만, 안전하게 재사용해도 됨
                st_present = ("status" in data) or ("status" in pdata)
                if not st_present:
                    _node_fail(cmd_key, "missing_status", mid=mid)
                    return

                st = int(data.get("status", pdata.get("status", 0)) or 0)

                # 노드 코드 기준: cmd 바이트가 0x10/0x11
                if st:       # ON
                    cmd_code = 0x10          # LIGHT_ON
                    flags2   = 0x01          # flags bit0=1 -> 실제 ON
                else:        # OFF
                    cmd_code = 0x11          # LIGHT_OFF
                    flags2   = 0x00

                tx_msg_id16 = int(msg_id_int) & 0xFFFF

                try:
                    self._pending_push(mid=mid, api_cmd="set_power_ctrl", srv_msg_id=raw_msg_id,
                                    want="ack", tx_msg_id=tx_msg_id16, ttl_sec=3.0)
                    data = payload.get("data") or {}
                    status = int(data.get("status", 0))  # 기본 0
                    status = 1 if status else 0          # 0/1로 정규화

                    extra = bytes([status])          
                    self.wisun.send_cmd_bytes(mid, cmd_code, msg_id=tx_msg_id16, flags=flags2, extra=extra)

                    self.log.info("DL cmd=set_power_ctrl mid=%d code=0x%02X flags=0x%02X status=%d msg_id=%s tx_msg_id=%d",
                        mid, cmd_code, flags2, status, raw_msg_id, tx_msg_id16)

                    _publish_node("set_power_ctrl", {
                        "cmd": "set_power_ctrl_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "msg_id": raw_msg_id,
                        "result": "success",
                        "queued": True,
                        "data": {"status": status},
                    })
                    _verbose_print(f"[CMD] set_power_ctrl → mid={mid} code=0x{cmd_code:02X} flags=0x{flags2:02X}")
                except Exception as e:
                    _node_fail("set_power_ctrl", f"send_error:{e}", mid=mid)

                return  


            elif cmd_key == "set_time_interval":
                period_min = int(data.get("period_min", payload.get("period_min", 0)) or 0)
                period_min = max(1, min(120, period_min))  
                extra = bytes([period_min & 0xFF])

            elif cmd_key == "set_node_info":
                info = data.get("info", payload.get("info", {}))
                s = json.dumps(info, ensure_ascii=False).encode("utf-8")
                if len(s) > 200:  
                    raise ValueError("info too long")
                extra = bytes([len(s) & 0xFF]) + s

            else:
                extra = b""

            tx_msg_id16 = int(msg_id_int) & 0xFFFF
            try:

                self._pending_push(
                    mid=mid,
                    api_cmd=cmd_key,
                    srv_msg_id=raw_msg_id,     # 서버에서 온 msg_id(문자열/원본)
                    want="ack",
                    tx_msg_id=tx_msg_id16,      # 노드로 보낸 msg_id(int)
                    ttl_sec=3.0
                )
                
                self.wisun.send_cmd_bytes(mid, cmd_code, msg_id=tx_msg_id16, flags=0x00, extra=extra)
                self.log.info("DL cmd=%s mid=%d code=0x%02X msg_id=%s tx_msg_id=%d extra_len=%d",
                cmd_key, mid, cmd_code, raw_msg_id, tx_msg_id16, len(extra))
                
                _publish_node(cmd_key, {
                    "cmd": f"{cmd_key}_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,
                    "result": "success",
                    "queued": True,
                })
                _verbose_print(f"[CMD] {cmd_key} → mid={mid} code=0x{cmd_code:02X} extra={extra.hex()}")
            except Exception as e:
                _node_fail(cmd_key, f"send_error:{e}", mid=mid)

        def _node_get_voltage_current_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_voltage_current", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)
            if not r or r.get("voltage") is None or r.get("current") is None:
                self._ack_fail("get_voltage_current", raw_msg_id, mid, "no_data")
                return
            clean = _sanitize_node_measurements(
                voltage=r.get("voltage"),
                current=r.get("current"),
                temperature=r.get("temperature"),
            )

            self._ack_ok("get_voltage_current", raw_msg_id, mid, {
                "voltage": clean["voltage"],
                "current": clean["current"],
                "temperature": clean["temperature"],
                "data_ts": r.get("ts"),
            })

        def _node_get_fft_data_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_fft_data", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)
            if not r or not r.get("fft"):
                self._ack_fail("get_fft_data", raw_msg_id, mid, "no_data")
                return
            clean = _sanitize_node_measurements(
                voltage=r.get("voltage"),
                current=r.get("current"),
                temperature=r.get("temperature"),
                fft=r.get("fft"),
            )

            self._ack_ok("get_fft_data", raw_msg_id, mid, {
                "fft": clean["fft"],
                "voltage": clean["voltage"],
                "current": clean["current"],
                "temperature": clean["temperature"],
                "data_ts": r.get("ts"),
            })

        def _node_get_node_uid_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            _verbose_print(f"[HANDLER] get_node_uid_cached enter mid={mid}", flush=True)

            if not mid:
                _node_fail("get_node_uid", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)
            uid = (r.get("uid") if r else None)

            if uid:  
                _verbose_print(f"[HANDLER] get_node_uid_cached HIT uid={uid}", flush=True)              
                _publish_node("get_node_uid", {
                    "cmd": "get_node_uid_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,
                    "result": "success",
                    "data": {"uid": uid},
                    "data_ts": int(r.get("ts") or 0) if isinstance(r, dict) else 0,
                })
                return

            _verbose_print("[HANDLER] get_node_uid_cached MISS -> forward", flush=True)
            _node_forward_simple("get_node_uid")

        def _node_get_channel_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_channel", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)
            ch = (r.get("ch") if r else None)   
            if ch is not None:
                _publish_node("get_channel", {
                    "cmd":"get_channel_ack","gid":self.gwid,"mid":mid,"msg_id":raw_msg_id,
                    "result":"success","data":{"ch": int(ch)}, "data_ts": int(r.get("ts") or 0)
                })
                return

            _node_forward_simple("get_channel")

        def _node_get_node_info_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_node_info", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)

            info = {}
            if isinstance(r, dict):
                clean = _sanitize_node_measurements(
                    voltage=r.get("voltage"),
                    current=r.get("current"),
                    temperature=r.get("temperature"),
                    light_on=r.get("light_on"),
                    fft=r.get("fft"),
                )
                # 흔히 있는 것들(있으면 넣고, 없으면 스킵)
                for k in (
                    "uid", "mac", "ch", "channel",
                    "online", "last_ts", "ts",
                    "gps", "latitude", "longitude",
                    "snap_period_min",
                    "ultrasonic",
                ):
                    if k in r and r[k] is not None:
                        info[k] = r[k]
                for k, v in clean.items():
                    if v is not None:
                        info[k] = v

            # 응답에 최소한 uid라도 있으면 "성공"으로 보고 반환
            if info:
                _publish_node("get_node_info", {
                    "cmd": "get_node_info_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,          
                    "result": "success",
                    "data": info,
                    "data_ts": int(info.get("ts") or info.get("last_ts") or 0),
                })
                return

            _node_forward_simple("get_node_info")

        def _node_get_status():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_node_status", "missing_mid")
                return

            tx_msg_id16 = int(msg_id_int) & 0xFFFF
            try:
                self._pending_push(
                    mid=mid,
                    api_cmd="get_node_status",
                    srv_msg_id=raw_msg_id,
                    want="ack",
                    tx_msg_id=tx_msg_id16,
                    ttl_sec=3.0
                )
                self.wisun.send_cmd_bytes(mid, CMD_GET_STATUS, msg_id=tx_msg_id16, flags=0x00, extra=b"")
                self.log.info("DL cmd=get_node_status mid=%d code=0x%02X msg_id=%s tx_msg_id=%d",
                            mid, CMD_GET_STATUS, raw_msg_id, tx_msg_id16)
                _verbose_print(f"[CMD] get_node_status → mid={mid} code=0x{CMD_GET_STATUS:02X} tx_msg_id={tx_msg_id16}")
            except Exception as e:
                _node_fail("get_node_status", f"send_error:{e}", mid=mid)

        """ def _node_set_setting():
            mid = int(payload.get("mid", 0) or 0)
            if not mid:
                _node_fail("set_setting", "missing_mid")
                return

            d = payload.get("payload") or payload.get("data") or {}

            def parse_time(s: str | None):
                if not s:
                    return 0, 0
                try:
                    p = s.split(":")
                    h = int(p[0])
                    m = int(p[1]) if len(p) > 1 else 0
                    return h, m
                except Exception:
                    return 0, 0

            saving_start_h, saving_start_m = parse_time(d.get("saving_start_time"))
            saving_end_h, saving_end_m = parse_time(d.get("saving_end_time"))

            on_off_mode     = int(d.get("on_off_mode", 0) or 0)
            on_corr_mode    = int(d.get("on_correction_mode", 0) or 0)
            on_corr_time    = int(d.get("on_correction_time", 0) or 0)
            off_corr_mode   = int(d.get("off_correction_mode", 0) or 0)
            off_corr_time   = int(d.get("off_correction_time", 0) or 0)
            forced_time     = int(d.get("forced_time", 0) or 0)
            saving_mode     = int(d.get("saving_mode", 0) or 0)
            snap_enable     = int(d.get("snap_enable", 1) or 1)
            snap_period_min = int(d.get("snap_period_min", 1) or 1)

            if snap_period_min <= 0 or snap_period_min > 120:
                snap_period_min = max(1, min(120, snap_period_min))

            base_extra = [
                on_off_mode           & 0xFF,
                on_corr_mode          & 0xFF,
                on_corr_time          & 0xFF,
                off_corr_mode         & 0xFF,
                off_corr_time         & 0xFF,
                forced_time           & 0xFF,
                saving_mode           & 0xFF,
                saving_start_h        & 0xFF,
                saving_start_m        & 0xFF,
                saving_end_h          & 0xFF,
                saving_end_m          & 0xFF,
                (1 if snap_enable else 0) & 0xFF,
                snap_period_min       & 0xFF,
                mid & 0xFF,
                (mid >> 8) & 0xFF,
            ]
            extra = bytes(base_extra)
            self._pending_push(
                    mid=mid,
                    api_cmd="set_setting",
                    srv_msg_id=raw_msg_id,
                    want="ack",
                    tx_msg_id=msg_id_int,
                    ttl_sec=3.0
                )
            self.wisun.send_cmd_bytes(mid, CMD_SET_SETTING, msg_id=msg_id_int, flags=0x00, extra=extra)
            print(f"[CMD] set_setting → mid={mid} extra={extra.hex()}") """
        def _node_apply_setting(api_cmd: str):
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail(api_cmd, "missing_mid")
                return

            d = payload.get("payload") or payload.get("data") or data or payload or {}

            def parse_u8(value, default: int = 0) -> int:
                try:
                    parsed = int(value if value is not None else default)
                except Exception:
                    parsed = default
                return max(0, min(255, parsed))

            def parse_time(s: str | None):
                if not s:
                    return 0, 0
                try:
                    text = str(s).strip()
                    if "T" in text:
                        text = text.split("T", 1)[1]
                    if text.endswith("Z"):
                        text = text[:-1]
                    if "." in text:
                        text = text.split(".", 1)[0]
                    parts = text.split(":")
                    h = int(parts[0])
                    m = int(parts[1]) if len(parts) > 1 else 0
                    h = max(0, min(23, h))
                    m = max(0, min(59, m))
                    return h, m
                except Exception:
                    return 0, 0

            on_h, on_m = parse_time(d.get("on_time"))
            off_h, off_m = parse_time(d.get("off_time"))
            saving_start_h, saving_start_m = parse_time(d.get("saving_start_time"))
            saving_end_h, saving_end_m     = parse_time(d.get("saving_end_time"))

            on_off_mode_raw = d.get("on_off_mode")
            if on_off_mode_raw is None:
                on_off_mode_raw = d.get("mode")

            on_off_mode     = parse_u8(on_off_mode_raw, 0)
            on_corr_mode    = parse_u8(d.get("on_correction_mode"), 0)
            on_corr_time    = parse_u8(d.get("on_correction_time"), 0)
            off_corr_mode   = parse_u8(d.get("off_correction_mode"), 0)
            off_corr_time   = parse_u8(d.get("off_correction_time"), 0)
            forced_time     = int(d.get("forced_time", 0) or 0)
            saving_mode     = parse_u8(d.get("saving_mode"), 0)
            snap_enable     = 1 if int(d.get("snap_enable", 1) or 1) else 0

            # 노드 프로토콜상 snap_period_min 바이트는 유지해야 하지만,
            # 서버 payload에서는 interval만 신뢰해서 채운다.

            # 서버 payload 호환:
            # 1) interval
            # 2) snap_period_min
            # 3) 기존 저장 interval
            # 4) 최종 기본값 60분
            snap_period_raw = (
                d.get("interval")
                if d.get("interval") is not None
                else payload.get("interval")
                if payload.get("interval") is not None
                else 60
            )

            snap_period_min = int(snap_period_raw or 60)
            snap_period_min = max(1, min(120, snap_period_min))

            forced_time = max(0, min(0xFFFF, forced_time))

            extra = bytes([
                on_off_mode & 0xFF,
                on_corr_mode & 0xFF,
                on_corr_time & 0xFF,
                off_corr_mode & 0xFF,
                off_corr_time & 0xFF,
                forced_time & 0xFF,
                (forced_time >> 8) & 0xFF,
                saving_mode & 0xFF,
                saving_start_h & 0xFF,
                saving_start_m & 0xFF,
                saving_end_h & 0xFF,
                saving_end_m & 0xFF,
                (1 if snap_enable else 0) & 0xFF,
                snap_period_min & 0xFF,
                on_h & 0xFF,
                on_m & 0xFF,
                off_h & 0xFF,
                off_m & 0xFF,
            ])
            tx_msg_id16 = int(msg_id_int) & 0xFFFF
            tx_meta = {
                "tx_on_off_mode": on_off_mode,
                "tx_on_time": f"{on_h:02d}:{on_m:02d}",
                "tx_off_time": f"{off_h:02d}:{off_m:02d}",
                "tx_on_correction_mode": on_corr_mode,
                "tx_on_correction_time": on_corr_time,
                "tx_off_correction_mode": off_corr_mode,
                "tx_off_correction_time": off_corr_time,
                "tx_forced_time": forced_time,
                "tx_saving_mode": saving_mode,
                "tx_saving_start_time": f"{saving_start_h:02d}:{saving_start_m:02d}",
                "tx_saving_end_time": f"{saving_end_h:02d}:{saving_end_m:02d}",
                "tx_snap_enable": 1 if snap_enable else 0,
                "tx_interval": snap_period_min,
                "tx_extra_hex": extra.hex(),
            }
            try:
                if self.store is not None:
                    rec = self._store_latest_by_mid(mid)
                    uid_for_store = rec.get("uid") if isinstance(rec, dict) else None
                    self.store.upsert(
                        uid=uid_for_store,
                        mid=mid,
                        interval=snap_period_min,
                    )

                self._pending_push(
                    mid=mid,
                    api_cmd=api_cmd,
                    srv_msg_id=raw_msg_id,
                    want="ack",
                    tx_msg_id=tx_msg_id16,
                    ttl_sec=3.0,
                    meta=tx_meta,
                )

                
                self.wisun.send_cmd_bytes(mid, CMD_SET_SETTING, msg_id=tx_msg_id16, flags=0x00, extra=extra)
                print(f"[CMD TX SET_SETTING] mid={mid} msg_id={tx_msg_id16} meta={tx_meta}", flush=True)

                
                _publish_node(api_cmd, {
                    "cmd": f"{api_cmd}_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": tx_msg_id16,
                    "result": "success",
                    "queued": True,
                    **tx_meta,
                })
                _verbose_print(f"[CMD_DBG] {api_cmd} mid={mid} tx={tx_meta}")

                _verbose_print(f"[CMD] {api_cmd} → mid={mid} (TX:0x31) extra={extra.hex()}")
            except Exception as e:
                _node_fail(api_cmd, f"send_error:{e}", mid=mid)

        def _node_set_setting():
            _verbose_print(f"[HANDLER] set_setting → mid={payload.get('mid')} msg_id={payload.get('msg_id')} topic={topic}")
            _node_apply_setting("set_setting")

        def _node_set_node_info():
            _verbose_print(f"[HANDLER] set_node_info → mid={payload.get('mid')} msg_id={payload.get('msg_id')} topic={topic}")
            _node_apply_setting("set_node_info")

        def _node_set_mid_chan():
            target_uid = (
                data.get("uid")
                or data.get("target_uid")
                or payload.get("uid")
                or payload.get("target_uid")
            )
            new_mid = data.get("mid")
            new_ch  = data.get("ch")

            if not target_uid:
                _node_fail("set_mid_chan", "missing_uid")
                return
            if new_mid is None or new_ch is None:
                _node_fail("set_mid_chan", "missing_mid_or_ch")
                return

            try:
                new_mid = int(new_mid)
                new_ch = int(new_ch)
            except Exception:
                _node_fail("set_mid_chan", "bad_mid_or_ch")
                return

            try:
                uid_bytes = bytes.fromhex(str(target_uid))
                if len(uid_bytes) != 12:
                    raise ValueError("uid must be 12 bytes")
            except Exception as e:
                _node_fail("set_mid_chan", f"bad_uid:{e}")
                return

            extra = bytearray()
            extra.extend(uid_bytes)
            extra.append((new_mid >> 8) & 0xFF)
            extra.append(new_mid & 0xFF)
            extra.append(new_ch & 0xFF)

            try:
                self.wisun.send_cmd_bytes(0, CMD_SET_MID_CH, msg_id=msg_id_int, flags=0x00, extra=bytes(extra))
                _publish_node("set_mid_chan", {
                    "cmd": "set_mid_chan_ack",
                    "gid": self.gwid,
                    "uid": uid_bytes.hex(),
                    "mid": new_mid,
                    "ch": new_ch,
                    "msg_id": raw_msg_id,
                    "result": "success",
                    "queued": True,
                })
                _verbose_print(f"[CMD] set_mid_chan → UID={uid_bytes.hex()} MID={new_mid} CH={new_ch}, msg_id={msg_id_int}")
            except Exception as e:
                _node_fail("set_mid_chan", f"send_error:{e}")

        def _node_setid_key():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("setid_key", "missing_mid")
                return
            key = (data.get("key") or "").encode("utf-8")
            try:
                self.wisun.send(mid, key)
                _publish_node("setid_key", {
                    "cmd": "setid_key_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,
                    "result": "success",
                })
            except Exception as e:
                _node_fail("setid_key", f"send_error:{e}", mid=mid)

        def _node_ping():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("ping", "missing_mid")
                return
            try:
                self.wisun.send(mid, b"PING")
                _publish_node("ping", {
                    "cmd": "ping_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,
                    "result": "success",
                    "queued": True,
                })
            except Exception as e:
                _node_fail("ping", f"send_error:{e}", mid=mid)

        def _node_get_all_status():
            self._handle_get_all_status(payload)

        node_handlers = {
            "get_node_status": _node_get_status,
            "set_setting": _node_set_setting,
            "set_node_info": _node_set_node_info, 
            "set_mid_chan": _node_set_mid_chan,
            "setid_key": _node_setid_key,
            "ping": _node_ping,
            "get_all_status": _node_get_all_status,
            
            "get_fft_data":         _node_get_fft_data_cached,
            "get_voltage_current":  _node_get_voltage_current_cached,
            "get_node_uid":         _node_get_node_uid_cached,
            "get_channel":          _node_get_channel_cached,
            "get_node_info":        lambda *_a, **_k: _node_forward_simple("get_node_info"),
            "set_power_ctrl":       lambda *_a, **_k: _node_forward_simple("set_power_ctrl"),
            "set_channel":          lambda *_a, **_k: _node_forward_simple("set_channel"),            
            "set_time_interval":    lambda *_a, **_k: _node_forward_simple("set_time_interval"),
        }

        h = node_handlers.get(node_cmd)
        if not h:
            _node_fail(node_cmd or "unknown", "unknown_node_cmd")
            return

        h()
        return

    def _now(self) -> float:
        return time.time()

    def _ensure_comm_health(self, mid: int) -> dict | None:
        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            return None
        if mid_int <= 0:
            return None

        with self._comm_lock:
            state = self.comm_health.get(mid_int)
            if state is None:
                state = {
                    "crc_error_count": 0,
                    "frame_error_count": 0,
                    "timeout_count": 0,
                    "unpack_error_count": 0,
                    "unknown_uplink_count": 0,
                    "last_error": None,
                    "last_error_detail": None,
                    "last_error_ts": None,
                    "last_ok_ts": None,
                }
                self.comm_health[mid_int] = state
            return state

    def _mark_node_pending(self, mid: int):
        if self.store is None or not hasattr(self.store, "mark_pending_by_mid"):
            return
        try:
            self.store.mark_pending_by_mid(mid, True)
        except Exception as e:
            self.log.warning("mark_pending_by_mid failed mid=%s err=%r", mid, e)

    def _comm_mark_success(self, mid: int, ts: int | None = None):
        state = self._ensure_comm_health(mid)
        if state is None:
            return
        now_ts = int(ts if ts is not None else time.time())
        with self._comm_lock:
            state["last_ok_ts"] = now_ts

    def _comm_mark_error(self, mid: int, kind: str, *,
                         ts: int | None = None, detail: str | None = None,
                         count_field: str | None = None, mark_pending: bool = True):
        state = self._ensure_comm_health(mid)
        if state is None:
            return
        now_ts = int(ts if ts is not None else time.time())
        with self._comm_lock:
            if count_field:
                state[count_field] = int(state.get(count_field, 0) or 0) + 1
            state["last_error"] = kind
            state["last_error_detail"] = detail
            state["last_error_ts"] = now_ts
        if mark_pending:
            self._mark_node_pending(mid)

    def _comm_health_payload(self, mid: int, *, now: int | None = None, rec: dict | None = None) -> dict:
        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            mid_int = 0

        now_ts = int(now if now is not None else time.time())
        if rec is None and mid_int > 0:
            rec = self._store_latest_by_mid(mid_int)

        with self._comm_lock:
            raw = dict(self.comm_health.get(mid_int, {}))
        last_error = raw.get("last_error")
        last_error_ts = int(raw.get("last_error_ts") or 0)
        last_ok_ts = int(raw.get("last_ok_ts") or 0)
        has_active_error = last_error_ts > last_ok_ts

        last_seen = 0
        online = False
        if rec is not None:
            last_seen, online = self._node_inventory_meta(rec, now=now_ts)

        faults = []
        if last_seen > 0 and not online:
            faults.append("no_recent_uplink")
        if has_active_error and int(raw.get("timeout_count", 0) or 0) > 0:
            faults.append("response_timeout")
        if has_active_error and last_error in ("ck_mismatch", "invalid_etx", "len_mismatch"):
            faults.append("frame_error")
        if has_active_error and int(raw.get("unpack_error_count", 0) or 0) > 0:
            faults.append("payload_unpack_error")
        if has_active_error and int(raw.get("unknown_uplink_count", 0) or 0) > 0:
            faults.append("unknown_uplink")

        faults = list(dict.fromkeys(faults))
        primary_fault = faults[0] if faults else None

        if primary_fault == "no_recent_uplink":
            state = "offline"
            error_level = "critical"
        elif primary_fault == "response_timeout":
            state = "degraded"
            error_level = "high"
        elif primary_fault in ("frame_error", "payload_unpack_error"):
            state = "degraded"
            error_level = "medium"
        elif primary_fault == "unknown_uplink":
            state = "degraded"
            error_level = "low"
        elif last_seen <= 0:
            state = "unknown"
            error_level = "none"
        else:
            state = "normal"
            error_level = "none"

        return {
            "state": state,
            "is_abnormal": state in ("offline", "degraded"),
            "error_code": primary_fault,
            "error_level": error_level,
            "faults": faults,
            "last_ok_ts": raw.get("last_ok_ts"),
            "last_seen_ts": int(last_seen or 0),
            "stats": {
                "crc_error_count": int(raw.get("crc_error_count", 0) or 0),
                "frame_error_count": int(raw.get("frame_error_count", 0) or 0),
                "timeout_count": int(raw.get("timeout_count", 0) or 0),
                "unpack_error_count": int(raw.get("unpack_error_count", 0) or 0),
                "unknown_uplink_count": int(raw.get("unknown_uplink_count", 0) or 0),
            },
        }

    def on_wisun_frame_error(self, event: dict):
        if not isinstance(event, dict):
            return
        mid = event.get("mid")
        if mid is None:
            return
        kind = str(event.get("kind") or "frame_error")
        count_field = "crc_error_count" if kind == "ck_mismatch" else "frame_error_count"
        self._comm_mark_error(
            mid,
            kind,
            ts=event.get("ts"),
            detail=event.get("detail"),
            count_field=count_field,
            mark_pending=True,
        )

    def _cache_put(self, kind: str, mid: int, msg: dict):
        self.cache[kind][mid] = (self._now(), msg)

    def _cache_get(self, kind: str, mid: int, max_age_sec: float):
        v = self.cache[kind].get(mid)
        if not v:
            return None
        ts, msg = v
        if (self._now() - ts) > max_age_sec:
            return None
        return msg

    def _should_drop_duplicate_uplink(self, kind: str, mid: int, fingerprint) -> bool:
        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            mid_int = 0
        if mid_int <= 0 or fingerprint is None:
            return False

        now = time.monotonic()
        window = float(getattr(self, "uplink_dedupe_window_sec", 2.0) or 2.0)
        if window <= 0:
            return False

        key = (str(kind), mid_int, fingerprint)
        with self._recent_uplink_lock:
            prev = self._recent_uplink.get(key)
            if prev is not None and (now - prev) <= window:
                return True

            self._recent_uplink[key] = now
            expire_before = now - max(window * 4.0, 10.0)
            if len(self._recent_uplink) > 512:
                stale_keys = [
                    k for k, seen_ts in self._recent_uplink.items()
                    if seen_ts < expire_before
                ]
                for stale_key in stale_keys:
                    self._recent_uplink.pop(stale_key, None)
        return False
    
    def _store_latest_by_mid(self, mid: int):
        if self.store is None:
            return None
        try:
            nodes = self.store.all_nodes()  # 이미 쓰고 있음
        except Exception:
            return None

        candidates = [
            r for r in nodes
            if int(r.get("mid") or 0) == int(mid)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.get("ts", 0))

    def _node_inventory_meta(self, rec: dict, now: int | None = None) -> tuple[int, bool]:
        if now is None:
            now = int(time.time())

        # Link/online status should follow the last uplink we heard from the node.
        # last_snap_ts is measurement freshness; an ok=0 snap must not make Wi-SUN look offline.
        last_ts = int(rec.get("last_ts") or rec.get("ts") or rec.get("last_snap_ts") or 0)
        mid = int(rec.get("mid") or 0)

        if mid <= 0 or last_ts <= 0:
            return last_ts, False

        online_window_sec = self._snap_online_window_sec(mid=mid)
        return last_ts, (now - last_ts) <= online_window_sec

    def _wisun_status_info(self, mid: int, now: int | None = None, rec: dict | None = None) -> dict:
        if now is None:
            now = int(time.time())

        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            mid_int = 0

        if mid_int <= 0:
            code = WISUN_STATUS_OFFLINE
            return {
                "wisun_status": code,
                "wisun_status_text": WISUN_STATUS_TEXT[code],
            }

        if rec is None:
            rec = self._store_latest_by_mid(mid_int)

        online_window_sec = self._snap_online_window_sec(mid=mid_int)
        online = False
        last_snap_ts = 0

        if rec is not None:
            _last_ts, online = self._node_inventory_meta(rec, now=now)
            last_snap_ts = int(rec.get("last_snap_ts") or 0)
        elif self.reg is not None and hasattr(self.reg, "is_online"):
            try:
                online = bool(self.reg.is_online(mid_int, now))
            except Exception:
                online = False

        if not online:
            code = WISUN_STATUS_OFFLINE
        else:
            comm_health = self._comm_health_payload(mid_int, now=now, rec=rec)
            if comm_health.get("state") == "degraded":
                code = WISUN_STATUS_DEGRADED
            else:
                code = WISUN_STATUS_OK

        return {
            "wisun_status": code,
            "wisun_status_text": WISUN_STATUS_TEXT[code],
        }

    def _wisun_status_value(self, mid: int, now: int | None = None) -> int:
        return int(self._wisun_status_info(mid, now=now)["wisun_status"])

    def _remember_good_measurements(self, mid: int, ts: int, clean: dict):
        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            return
        if mid_int <= 0:
            return

        prev = self._last_good_measurements.get(mid_int, {})
        rec = dict(prev)
        rec["ts"] = ts
        if clean.get("temperature") is not None:
            rec["temperature"] = clean["temperature"]
        complete_fft = _complete_fft_or_none(clean.get("fft"))
        if complete_fft is not None:
            rec["fft"] = complete_fft
        if rec.get("temperature") is not None or rec.get("fft") is not None:
            self._last_good_measurements[mid_int] = rec

    def _measurement_fallback(self, mid: int, prev_rec: dict | None = None) -> dict:
        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            return {}
        if mid_int <= 0:
            return {}

        fallback = dict(self._last_good_measurements.get(mid_int, {}))
        if prev_rec:
            store_good = _sanitize_node_measurements(
                temperature=prev_rec.get("last_good_temperature"),
                fft=_complete_fft_or_none(prev_rec.get("last_good_fft")),
            )
            if fallback.get("temperature") is None and store_good["temperature"] is not None:
                fallback["temperature"] = store_good["temperature"]
            if fallback.get("fft") is None and store_good["fft"] is not None:
                fallback["fft"] = store_good["fft"]
        return fallback

    def _log_wisun_status_transition(
        self,
        *,
        mid: int,
        uid: str | None,
        mac: str | None,
        now: int,
        last_ts: int,
        last_snap_ts: int,
        online_window_sec: int,
        status_info: dict,
    ) -> None:
        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            return
        if mid_int <= 0:
            return

        new_code = int(status_info.get("wisun_status", WISUN_STATUS_OFFLINE))
        prev_code = self._last_wisun_status.get(mid_int)
        self._last_wisun_status[mid_int] = new_code
        if prev_code == new_code:
            return

        last_age_sec = None if last_ts <= 0 else max(0, now - last_ts)
        snap_age_sec = None if last_snap_ts <= 0 else max(0, now - last_snap_ts)
        print(
            f"[WISUN_STATUS_CHANGE] mid={mid_int} uid={uid} mac={mac} "
            f"from={WISUN_STATUS_TEXT.get(prev_code, 'unknown') if prev_code is not None else 'init'} "
            f"to={status_info.get('wisun_status_text')} "
            f"last_age_sec={last_age_sec} snap_age_sec={snap_age_sec} "
            f"last_ts={last_ts} last_snap_ts={last_snap_ts} window={online_window_sec}",
            flush=True,
        )

    def _remember_node_seen(
        self,
        *,
        mid: int,
        ts: int,
        uid: str | None = None,
        mac: str | None = None,
        voltage=None,
        current=None,
        temperature=None,
        light_on=None,
        fft=None,
        ai_valid=None,
        ai_mse=None,
        ai_pred=None,
        source: str | None = None,
        update_last_snap_ts: bool = True,
    ):
        if self.store is None:
            return None

        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            return None

        if mid_int <= 0:
            return None

        clean = _sanitize_node_measurements(
            voltage=voltage,
            current=current,
            temperature=temperature,
            light_on=light_on,
            fft=fft,
        )
        clean_ai = _sanitize_ai_result(
            ai_valid=ai_valid,
            ai_mse=ai_mse,
            ai_pred=ai_pred,
        )
        if any(v is not None for v in (voltage, current, temperature, light_on, fft)):
            dropped = [
                name for name, raw in (
                    ("voltage", voltage),
                    ("current", current),
                    ("temperature", temperature),
                    ("light_on", light_on),
                    ("fft", fft),
                )
                if raw is not None and clean.get(name) is None
            ]
            if dropped:
                _verbose_print(
                    f"[SANITIZE NODE] mid={mid_int} dropped={dropped} "
                    f"raw={{'voltage': {voltage}, 'current': {current}, "
                    f"'temperature': {temperature}, 'light_on': {light_on}, 'fft': {fft}}}",
                    flush=True,
                )
        if any(v is not None for v in (ai_valid, ai_mse, ai_pred)):
            dropped_ai = [
                name for name, raw in (
                    ("ai_valid", ai_valid),
                    ("ai_mse", ai_mse),
                    ("ai_pred", ai_pred),
                )
                if raw is not None and clean_ai.get(name) is None
            ]
            if dropped_ai:
                _verbose_print(
                    f"[SANITIZE AI] mid={mid_int} dropped={dropped_ai} "
                    f"raw={{'ai_valid': {ai_valid}, 'ai_mse': {ai_mse}, 'ai_pred': {ai_pred}}}",
                    flush=True,
                )

        prev = self._store_latest_by_mid(mid_int)
        prev_sig = None if prev is None else (
            int(prev.get("mid") or 0),
            prev.get("uid"),
            prev.get("mac"),
        )

        update_source = source or ("snap" if any(v is not None for v in clean.values()) else "identity")
        uid_to_store = uid
        if prev is not None and uid is not None and update_source not in ("snap", "event"):
            prev_uid = prev.get("uid")
            if prev_uid and prev_uid != uid:
                _verbose_print(
                    f"[NODE_SEEN UID GUARD] mid={mid_int} source={update_source} "
                    f"ignore_uid={uid} keep_uid={prev_uid} mac={mac} prev_mac={prev.get('mac')}",
                    flush=True,
                )
                uid_to_store = prev_uid

        has_measure = any(v is not None for v in clean.values())
        if has_measure and update_last_snap_ts:
            self._remember_good_measurements(mid_int, ts, clean)
        try:
            good_temperature = clean["temperature"] if has_measure and update_last_snap_ts else None
            good_fft = (
                _complete_fft_or_none(clean["fft"])
                if has_measure and update_last_snap_ts
                else None
            )
            good_measurement_ts = ts if (good_temperature is not None or good_fft is not None) else None
            rec = self.store.upsert(
                uid=uid_to_store,
                mid=mid_int,
                mac=mac,
                ultrasonic=None,
                voltage=clean["voltage"],
                current=clean["current"],
                temperature=clean["temperature"],
                light_on=clean["light_on"],
                fft=clean["fft"],
                ai_valid=clean_ai["ai_valid"],
                ai_mse=clean_ai["ai_mse"],
                ai_pred=clean_ai["ai_pred"],
                ts=ts,
                pending_send=True,
                last_snap_ts=ts if has_measure and update_last_snap_ts else None,
                last_good_temperature=good_temperature,
                last_good_fft=good_fft,
                last_good_measurement_ts=good_measurement_ts,
            )
        except Exception as e:
            print("[NodeStore] update error(remember_node_seen):", e)
            return prev

        if not rec:
            return prev

        self._comm_mark_success(mid_int, ts)
        _verbose_print(
            f"[NODE_SEEN] mid={mid_int} source={update_source} ts={ts} "
            f"uid={rec.get('uid')} has_measure={has_measure} last_snap_ts={rec.get('last_snap_ts')}",
            flush=True,
        )

        cur_sig = (
            int(rec.get("mid") or 0),
            rec.get("uid"),
            rec.get("mac"),
        )
        if prev_sig != cur_sig and self.mqtt is not None:
            try:
                self.publish_node_inventory(reason="uplink_seen")
            except Exception as e:
                print("[INV] publish error(uplink_seen):", e)

        return rec

    def publish_node_inventory(self, reason: str = "mqtt_connect"):
        nodes = self.store.all_nodes() if self.store is not None else []
        now = int(time.time())

        items = []
        for r in nodes:
            mid = int(r.get("mid") or 0)
            if mid == 0:
                continue
            last_ts, online = self._node_inventory_meta(r, now=now)
            status_info = self._wisun_status_info(mid, now=now, rec=r)
            items.append({
                "mid": mid,
                "uid": r.get("uid"),
                "mac": r.get("mac"),
                **status_info,
                "last_ts": last_ts,
                "comm_health": self._comm_health_payload(mid, now=now, rec=r),
            })

        # mid 기준 정렬(보기 좋게)
        items.sort(key=lambda x: x["mid"])

        payload = {
            "cmd": "node_inventory",
            "gid": self.gwid,
            "ts": int(time.time()),
            "reason": reason,
            "nodes": items,
            "count": len(items),
        }

        topic = f"gw/{self.gwid}/mid_lists"  
        self.mqtt.publish_json(topic, payload)
        _verbose_print(f"[MQTT TX INVENTORY] topic={topic} count={len(items)}")


    def on_node_inventory(self, payload: dict):
        # nodes: [{mid, uid, mac, wisun_status, last_ts}, ...]
        nodes = payload.get("nodes", [])
        reason = payload.get("reason")
        ts = payload.get("ts")
        gid = payload.get("gid")

        # 여기서 nodes_store.bin 또는 in-memory store seed/update
        # self.store.seed_inventory(gid, nodes, ts=ts, reason=reason)
        _verbose_print(f"[INV] seed ok gid={gid} count={len(nodes)} reason={reason} ts={ts}")

    def _publish_node_ack(self, api_cmd: str, resp: dict):
        # 네 규칙대로 topic을 맞춰
        topic = f"node/{self.gwid}/response/{api_cmd}"
        self.mqtt.publish_json(topic, resp)
        _verbose_print("[MQTT TX ACK]", topic, resp)

    def _ack_ok(self, api_cmd: str, msg_id, mid: int, data: dict):
        self._publish_node_ack(api_cmd, {
            "cmd": f"{api_cmd}_ack",
            "gid": self.gwid,
            "mid": mid,
            "msg_id": msg_id,
            "result": "success",
            "data": data,
        })

    def _ack_fail(self, api_cmd: str, msg_id, mid: int, reason: str):
        self._publish_node_ack(api_cmd, {
            "cmd": f"{api_cmd}_ack",
            "gid": self.gwid,
            "mid": mid,
            "msg_id": msg_id,
            "result": "fail",
            "reason": reason,
        })

    def _pending_push(self, mid: int, api_cmd: str, srv_msg_id, want: str,
                      tx_msg_id: int | None = None, ttl_sec: float = 2.0,
                      meta: dict | None = None):
        self.pending[mid].append({
            "api": api_cmd,
            "srv_msg_id": srv_msg_id,
            "want": want,
            "tx_msg_id": tx_msg_id,
            "t": self._now(),
            "ttl": float(ttl_sec),
            "meta": dict(meta or {}),
        })

    def _expire_pending_for_mid(self, mid: int, now: float | None = None):
        q = self.pending.get(mid)
        if not q:
            return []

        now_ts = self._now() if now is None else now
        expired_items = []
        while q and (now_ts - q[0]["t"]) > q[0]["ttl"]:
            expired_items.append(q.popleft())
        return expired_items

    def _handle_expired_pending(self, mid: int, expired_items):
        for expired in expired_items:
            if expired.get("want") != "ack":
                continue
            self._ack_fail(expired["api"], expired.get("srv_msg_id"), mid, "timeout")
            self._comm_mark_error(
                mid,
                "response_timeout",
                detail=f"api={expired.get('api')}",
                count_field="timeout_count",
                mark_pending=True,
            )

    def _pending_pop_ack(self, mid: int, tx_msg_id: int | None):
        q = self.pending.get(mid)
        if not q:
            return None

        now = self._now()

        # timeout 정리
        expired_items = self._expire_pending_for_mid(mid, now=now)
        if expired_items:
            self._handle_expired_pending(mid, expired_items)
            
        # tx_msg_id가 없으면 첫 ack 대기만 pop
        if tx_msg_id is None:
            for _ in range(len(q)):
                item = q[0]
                if item.get("want") == "ack":
                    return q.popleft()
                q.rotate(-1)
            return None

        # tx_msg_id로 정확 매칭
        for _ in range(len(q)):
            item = q[0]
            if item.get("want") == "ack" and item.get("tx_msg_id") == tx_msg_id:
                return q.popleft()
            q.rotate(-1)

        return None    

    def _pending_watchdog_loop(self):
        while True:
            time.sleep(1.0)
            try:
                mids = list(self.pending.keys())
                now = self._now()
                for mid in mids:
                    expired_items = self._expire_pending_for_mid(mid, now=now)
                    if expired_items:
                        self._handle_expired_pending(mid, expired_items)
            except Exception as e:
                self.log.warning("pending watchdog error: %r", e)

    def _snap_batch_loop(self):
        while not self._stop_event.is_set():
            wait_sec = float(getattr(self, "snap_batch_period_sec", 60.0) or 60.0)
            if wait_sec <= 0:
                wait_sec = 1.0
            self._stop_event.wait(wait_sec)
            if self._stop_event.is_set():
                break
            try:
                self._publish_snap_batch()
            except Exception as e:
                print("[SNAP_BATCH] error:", e)

    

    def _handle_get_all_status(self, req: dict):
        """
        서버에서 get_all_status 요청 왔을 때 처리.
        NodeStore에 저장된 값 기준으로 전체 노드 상태 응답.
        """
        if self.store is None:
            return

        nodes = self.store.all_nodes()
        if not nodes:
            # 노드 하나도 없는 경우
            resp = {
                "cmd": "get_all_status",
                "gid": self.gwid,
                "ts": int(time.time()),
                "nodes": [],
            }
            topic = f"node/{self.gwid}/get_all_status"
            self.mqtt.publish_json(topic, resp)
            _verbose_print("[MQTT TX GET_ALL_STATUS empty]", resp)
            return

        now = int(time.time())

        # mid 기준 정렬
        nodes_by_mid = {}
        for r in nodes:
            try:
                mid_key = int(r.get("mid") or 0)
            except (TypeError, ValueError):
                mid_key = 0
            if mid_key <= 0:
                continue
            old = nodes_by_mid.get(mid_key)
            if old is None or float(r.get("ts") or 0) >= float(old.get("ts") or 0):
                nodes_by_mid[mid_key] = r

        nodes_sorted = sorted(
            nodes_by_mid.values(),
            key=lambda r: ((r.get("mid") or 0), (r.get("uid") or ""))
        )

        # reg(심박/마지막 접속시간) 같은 걸로 online 여부 판별 가능하면 같이 붙이기
        nodes_payload = []
        for r in nodes_sorted:
            mid = r.get("mid")
            uid = r.get("uid")
            last_ts = int(r.get("ts") or 0)

            # 예: 마지막 접속 2분 이내면 online 으로 표시
            online_window_sec = self._snap_online_window_sec()
            online = (now - last_ts) <= online_window_sec if last_ts > 0 else False

            status_info = self._wisun_status_info(mid, now=now, rec=r)
            clean = _sanitize_node_measurements(
                voltage=r.get("voltage"),
                current=r.get("current"),
                temperature=r.get("temperature"),
                light_on=r.get("light_on"),
                fft=r.get("fft"),
            )
            nodes_payload.append({
                "mid":   mid,
                "uid":   uid,
                "mac":   r.get("mac"),
                "last_ts": last_ts,
                "last_snap_ts": int(r.get("last_snap_ts") or 0),
                "last_good_measurement_ts": int(r.get("last_good_measurement_ts") or 0),
                **status_info,
                "pending_send": int(r.get("pending_send", 0) or 0),
                "comm_health": self._comm_health_payload(mid, now=now, rec=r),
                "data": {
                    "voltage":     clean["voltage"],
                    "current":     clean["current"],
                    "temperature": clean["temperature"],
                    "light_on":    clean["light_on"],
                    "fft":         clean["fft"],
                    "ai_valid":    r.get("ai_valid"),
                    "ai_mse":      r.get("ai_mse"),
                    "ai_pred":     r.get("ai_pred"),
                    "measurement_source": "store_current_unverified",
                },
            })

        resp = {
            "cmd": "get_all_status",
            "gid": self.gwid,
            "ts": now,
            "nodes": nodes_payload,
            # 필요하면 요청 쪽 msg_id / rqid 같은 것도 그대로 되돌려주기
            "msg_id": req.get("msg_id"),
        }

        topic = f"node/{self.gwid}/get_all_status"
        self.mqtt.publish_json(topic, resp)
        _verbose_print("[MQTT TX GET_ALL_STATUS]", resp)

    def _publish_snap_batch(self):
        """
        NodeStore 기준으로 전체 노드 상태를 mid 순으로 정렬해서
        node/{gwid}/snap_batch 토픽으로 한 번에 전송.
        이번 라운드에서 보고 안 한 노드는 online=False로 표시.
        """
        if self.store is None:
            return

        # 배치 전송은 pending 여부와 무관하게 현재 저장된 전체 노드 상태를
        # 설정된 주기마다 다시 publish한다.
        nodes = self.store.all_nodes()
        if not nodes:
            return

        now = int(time.time())

        # 이번 라운드에서 스냅을 보낸 노드 목록 (mid 집합)
        with self._snap_cycle_lock:
            seen_mids = set(self._snap_cycle_seen_mids)
            # 새 라운드를 위해 초기화
            self._snap_cycle_seen_mids.clear()
            self._snap_cycle_start_ts = now

        # mid, uid 기준으로 정렬
        nodes_sorted = sorted(
            nodes,
            key=lambda r: ((r.get("mid") or 0), (r.get("uid") or ""))
        )

        nodes_payload = []
        for r in nodes_sorted:
            mid = r.get("mid")
            uid = r.get("uid")
            # 이번 라운드에서 보고했는지 여부
            last_ts = int(r.get("ts") or 0)
            last_snap_ts = int(r.get("last_snap_ts") or 0)
            last_age_sec = None if last_ts <= 0 else max(0, now - last_ts)
            snap_age_sec = None if last_snap_ts <= 0 else max(0, now - last_snap_ts)
            online_window_sec, window_source, base_interval = self._snap_online_window_detail(mid=mid)
            is_online = (now - last_snap_ts) <= online_window_sec if last_snap_ts > 0 else False
            status_info = self._wisun_status_info(mid, now=now, rec=r)
            wisun_ok = int(status_info.get("wisun_status", WISUN_STATUS_OFFLINE)) == WISUN_STATUS_OK
            _verbose_print(
                f"[SNAP_BATCH NODE] mid={mid} uid={uid} mac={r.get('mac')} "
                f"last_ts={last_ts} last_snap_ts={last_snap_ts} window={online_window_sec} source={window_source} "
                f"stored_interval={r.get('interval')} base_interval={base_interval} "
                f"last_age_sec={last_age_sec} snap_age_sec={snap_age_sec} online={is_online} wisun_ok={wisun_ok}",
                flush=True,
            )
            self._log_wisun_status_transition(
                mid=mid,
                uid=uid,
                mac=r.get("mac"),
                now=now,
                last_ts=last_ts,
                last_snap_ts=last_snap_ts,
                online_window_sec=online_window_sec,
                status_info=status_info,
            )
            clean = _sanitize_node_measurements(
                voltage=r.get("voltage"),
                current=r.get("current"),
                temperature=r.get("temperature"),
                light_on=r.get("light_on"),
                fft=r.get("fft"),
            )

            node_obj = {
                "mid":   mid,
                "uid":   uid,
                "mac":   r.get("mac"),
                "last_ts": last_ts,
                "last_snap_ts": last_snap_ts,
                "last_good_measurement_ts": int(r.get("last_good_measurement_ts") or 0),
                "last_age_sec": last_age_sec,
                "snap_age_sec": snap_age_sec,
                **status_info,
                "pending_send": int(r.get("pending_send", 0) or 0),
                "status": "ok" if wisun_ok else "disconnected",
                "comm_health": self._comm_health_payload(mid, now=now, rec=r),
                "data": {
                    "voltage":     clean["voltage"],
                    "current":     clean["current"],
                    "temperature": clean["temperature"],
                    "light_on":    clean["light_on"],
                    "fft":         clean["fft"],
                    "ai_valid":    r.get("ai_valid"),
                    "ai_mse":      r.get("ai_mse"),
                    "ai_pred":     r.get("ai_pred"),
                    "measurement_source": "store_current_unverified",
                },
            }
            nodes_payload.append(node_obj)

        payload = {
            "cmd": "snap_batch",
            "gid": self.gwid,
            "ts": now,
            "nodes": nodes_payload,
        }

        topic_batch = f"node/{self.gwid}/snap"
        self.mqtt.publish_json(topic_batch, payload)
        self.store.mark_sent_for_mids(
            [int(node.get("mid") or 0) for node in nodes_payload],
            sent_ts=now,
        )
        print(f"[MQTT TX SNAP_BATCH] topic={topic_batch} payload={payload}", flush=True)

    def _alloc_mid(self) -> int:
        m = self.next_mid
        self.next_mid = (self.next_mid + 1) & 0xFFFF
        if self.next_mid == 0:
            self.next_mid = 1
        return m
    
    def on_wisun_uplink(self, mid: int, payload, ts: int, mac: str | None = None):
        try:
            self.reg.touch(mid, ts)
            raw_hex = payload.hex(" ") if isinstance(payload, (bytes, bytearray)) else ""
            _verbose_print(f"[GW] UL mid={mid} len={len(payload)} p0=0x{payload[0]:02X} mac={mac} raw={raw_hex}")
            topic_snap = f"node/{self.gwid}/snap"
            topic_light_state = f"node/{self.gwid}/light_state"
            topic_cmd  = lambda cmd: f"node/{self.gwid}/response/{cmd}"
            topic_raw  = f"gw/{self.gwid}/raw"
            looks_like_transport = False
            msg = None
            body = payload
            target_mid = None
            ttl = None
            cmd_code = None
            flags = None
            node_msg_id = None
            if isinstance(payload, (bytes, bytearray)):
                b = bytes(payload)  
                body = b
                if len(body) >= NODEINFO_SIZE and body[0] == T_NODEINFO_BIN:
                    info = parse_nodeinfo_bin(body)
                    self._remember_node_seen(
                        mid=mid,
                        ts=ts,
                        uid=info["uid"],
                        mac=mac or info.get("mac"),
                    )
                    resp = {
                        "cmd": "get_node_info_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": info["uid"],
                        "msg_id": info["msg_id"],
                        "node_msg_id": node_msg_id,
                        "result": "success" if info["ok"] else "fail",
                        "err_code": 0 if info["ok"] else 1,
                        "ts": ts,
                        "data": info,
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    self.mqtt.publish_json(topic_cmd("get_node_info"), resp)
                    _verbose_print("[MQTT TX GET_NODE_INFO_BIN]", resp)
                    return       
                # 0) 먼저: transport header 없는 순수 Ack/Snap인지 체크 (가장 안전)
                if len(payload) >= ACK_BIN_SIZE and payload[0] in (ACK_T, ACK_NODE_CFG_T):
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                elif len(payload) >= SNAP_BIN_SIZE and payload[0] == T_SNAP:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                elif len(payload) >= LIGHT_STATE_EVENT_BIN_SIZE and payload[0] == T_LIGHT_STATE_EVENT:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                elif len(payload) >= STATUS_BIN_SIZE_V1 and payload[0] == STATUS_T:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None 
                elif len(payload) >= GET_CH_BIN_SIZE and payload[0] == GET_CH_T:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                elif len(payload) >= NODE_INFO_HDR_SIZE and payload[0] == NODE_INFO_T:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                elif len(payload) >= 19 and payload[0] == CMD_SET_RTC_KST:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                else:
                    # 1) transport header 분리 시도
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                    
                    if len(payload) >= 7:
                        target_mid  = payload[0] | (payload[1] << 8)
                        ttl         = payload[2]
                        cmd_code    = payload[3]
                        flags       = payload[4]
                        node_msg_id = (payload[5] << 8) | payload[6]
                        body        = payload[7:]

                    if len(body) >= 1 and body[0] == T_SNAP:
                        post_freq0_hex = (
                            body[SNAP_POST_FREQ0_OFFSET:].hex(" ")
                            if len(body) > SNAP_POST_FREQ0_OFFSET else ""
                        )
                        cmd_text = f"0x{cmd_code:02X}" if cmd_code is not None else "None"
                        flags_text = f"0x{flags:02X}" if flags is not None else "None"
                        _verbose_print(
                            f"[GW RX RAW SNAP] mid={mid} len={len(payload)} "
                            f"target_mid={target_mid} ttl={ttl} cmd={cmd_text} flags={flags_text} "
                            f"node_msg_id={node_msg_id} body_len={len(body)} "
                            f"body_hex={body.hex(' ')} post_freq0_hex={post_freq0_hex}",
                            flush=True,
                        )

                    
                    is_body_ack  = (len(body) >= ACK_BIN_SIZE  and body[0] in (ACK_T, ACK_NODE_CFG_T))
                    is_body_snap = (len(body) >= SNAP_BIN_SIZE and body[0] == T_SNAP)
                    is_body_light_state = (len(body) >= LIGHT_STATE_EVENT_BIN_SIZE and body[0] == T_LIGHT_STATE_EVENT)
                    is_body_status = (len(body) >= STATUS_BIN_SIZE_V1 and body[0] == STATUS_T)
                    is_body_getch  = (len(body) >= 1 and body[0] == 0x24)
                    is_body_info   = (len(body) >= 1 and body[0] == 0x40)
                    is_body_rtc    = (len(body) >= 19 and body[0] == CMD_SET_RTC_KST)
                    
                    if not (is_body_ack or is_body_snap or is_body_light_state or is_body_status or is_body_getch or is_body_info or is_body_rtc):
                        
                        if (ttl is not None and 1 <= ttl <= 20 and
                            cmd_code is not None and cmd_code in (0x31,)):
                            print(f"[GW] DROP echoed/downlink-like uplink mid={mid} "
                                f"target_mid={target_mid} ttl={ttl} cmd=0x{cmd_code:02X} "
                                f"len={len(payload)}")
                            _verbose_print("[GW] dropped echoed raw=", raw_hex)
                            return            
                if len(body) >= LIGHT_STATE_EVENT_BIN_SIZE and body[0] == T_LIGHT_STATE_EVENT:
                    try:
                        event = unpack_light_state_event_bin(body)
                        if event is None:
                            raise struct.error("invalid light_state_event payload")
                    except struct.error as e:
                        print("[GW] LightStateEvent unpack error:", e, "len=", len(payload))
                        _verbose_print("[GW] LightStateEvent raw=", raw_hex)
                        self._comm_mark_error(
                            mid,
                            "light_state_event_unpack_error",
                            ts=ts,
                            detail=str(e),
                            count_field="unpack_error_count",
                            mark_pending=True,
                        )
                        self.mqtt.publish_json(topic_raw, {
                            "cmd": "light_state_event_unpack_error",
                            "gid": self.gwid,
                            "mid": mid,
                            "ts": ts,
                            "error": str(e),
                            "payload_hex": raw_hex,
                        })
                        return

                    uid_str = event["uid_bytes"].hex()
                    valid_flags = int(event["valid_flags"])
                    valid_light = bool(valid_flags & 0x01)
                    valid_vi = bool(valid_flags & 0x02)
                    valid_temp = bool(valid_flags & 0x04)
                    valid_fft = bool(valid_flags & 0x08)
                    valid_rtc = bool(valid_flags & 0x10) and "rtc_year" in event

                    light_on_val = event["light_on"] if valid_light else None
                    voltage_val = event["voltage"] if valid_vi else None
                    current_val = event["current"] if valid_vi else None
                    temp_val = event["temp"]
                    fft_val = [list(pair) for pair in event["fft_pairs"]]
                    rtc_val = event.get("rtc") if valid_rtc else None
                    fallback_from_snap = []
                    prev_rec = self._store_latest_by_mid(mid)
                    fallback_measure = self._measurement_fallback(mid, prev_rec)
                    if fallback_measure:
                        temp_clean_now = _bounded_number(temp_val, "temperature")
                        temp_looks_empty = (
                            temp_clean_now is None
                            or (
                                valid_temp
                                and abs(float(temp_clean_now)) < NODE_ZERO_EPS
                                and abs(float(event["temp"])) < NODE_ZERO_EPS
                            )
                        )
                        if temp_looks_empty and fallback_measure.get("temperature") is not None:
                            temp_val = fallback_measure["temperature"]
                            fallback_from_snap.append("temperature")

                        if _fft_missing_value(fft_val) and fallback_measure.get("fft"):
                            fft_val = fallback_measure["fft"]
                            fallback_from_snap.append("fft")

                    if fallback_from_snap:
                        _verbose_print(
                            f"[GW LIGHT_STATE_EVENT FALLBACK] mid={mid} uid={uid_str} "
                            f"fields={fallback_from_snap} temp={temp_val} fft={fft_val}",
                            flush=True,
                        )
                    if self._should_drop_duplicate_uplink("light_state_event", mid, bytes(body)):
                        _verbose_print(
                            f"[GW DEDUPE] drop duplicate light_state_event mid={mid} "
                            f"uid={uid_str} event_id={event['event_id']}",
                            flush=True,
                        )
                        return
                    _verbose_print(
                        f"[GW LIGHT_STATE_EVENT PARSED] mid={mid} uid={uid_str} "
                        f"event_id={event['event_id']} flags=0x{valid_flags:02X} "
                        f"layout={event.get('parse_layout')} score={event.get('parse_score')} "
                        f"valid_temp={valid_temp} valid_fft={valid_fft} "
                        f"temp_raw={event['temp']} fft_count_raw={event['fft_count']} "
                        f"fft_raw={fft_val} rtc={rtc_val} body_hex={body.hex(' ')}",
                        flush=True,
                    )
                    clean = _sanitize_node_measurements(
                        voltage=voltage_val,
                        current=current_val,
                        temperature=temp_val,
                        light_on=light_on_val,
                        fft=fft_val,
                    )

                    self._remember_node_seen(
                        mid=mid,
                        ts=ts,
                        uid=uid_str,
                        mac=mac,
                        voltage=voltage_val,
                        current=current_val,
                        temperature=temp_val,
                        light_on=light_on_val,
                        fft=fft_val,
                        source="event",
                        update_last_snap_ts=False,
                    )

                    resp = {
                        "cmd": "light_state_event",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "event_id": event["event_id"],
                        "valid_flags": valid_flags,
                        "valid": {
                            "light_on": valid_light,
                            "voltage_current": valid_vi,
                            "temperature": valid_temp,
                            "fft": valid_fft,
                            "rtc": valid_rtc,
                        },
                        "light_on": clean["light_on"],
                        "mode": event["mode"],
                        "reason": event["reason"],
                        "tick_ms": event["tick_ms"],
                        "parse_layout": event.get("parse_layout"),
                        "fallback_from_snap": fallback_from_snap,
                        "ts": ts,
                        "temperature": clean["temperature"],
                        "fft_count": len(clean["fft"] or []),
                        "fft": clean["fft"] or [],
                        "data": {
                            "voltage": clean["voltage"],
                            "current": clean["current"],
                            "temperature": clean["temperature"],
                            "fft_count": len(clean["fft"] or []),
                            "fft": clean["fft"] or [],
                            "fallback_from_snap": fallback_from_snap,
                            "rtc": rtc_val,
                            "rtc_synced": event.get("rtc_synced") if valid_rtc else None,
                        },
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    self.mqtt.publish_json(topic_light_state, resp)
                    _verbose_print("[MQTT TX LIGHT_STATE_EVENT]", resp)
                    return

                if len(body) >= GET_CH_BIN_SIZE and body[0] == GET_CH_T:
                    try:
                        t_val, uid_bytes, msg_id16, ok, err_code, ch = struct.unpack(GET_CH_BIN_FMT, body[:GET_CH_BIN_SIZE])
                    except struct.error as e:
                        print("[GW] GetChResp unpack error:", e, "len=", len(payload))
                        _verbose_print("[GW] GetChResp raw=", raw_hex)
                        return

                    uid_str = uid_bytes.hex()
                    self._remember_node_seen(mid=mid, ts=ts, uid=uid_str, mac=mac)
                    match_id = int(node_msg_id) if node_msg_id is not None else int(msg_id16)

                    # pending 있으면 서버 msg_id로 치환, 없으면 match_id 사용
                    pend = self._pending_pop_ack(mid, match_id)
                    srv_msg_id = (pend["srv_msg_id"] if pend else match_id)

                    resp = {
                        "cmd": "get_channel_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "msg_id": srv_msg_id,
                        "node_msg_id": match_id,
                        "result": "success" if ok else "fail",
                        "err_code": int(err_code),
                        "ts": ts,
                        "data": {"ch": int(ch)},
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    self.mqtt.publish_json(topic_cmd("get_channel"), resp)
                    _verbose_print("[MQTT TX GET_CHANNEL_ACK]", resp)
                    return
                if len(body) >= NODE_INFO_HDR_SIZE and body[0] == NODE_INFO_T:
                    try:
                        t_val, uid_bytes, msg_id16, ok, err_code, text_len = struct.unpack(
                            NODE_INFO_HDR_FMT, body[:NODE_INFO_HDR_SIZE]
                        )
                    except struct.error as e:
                        print("[GW] NodeInfoHdr unpack error:", e, "len=", len(payload))
                        _verbose_print("[GW] NodeInfoHdr raw=", raw_hex)
                        return

                    # 길이 방어
                    if text_len > NODE_INFO_TEXT_MAX:
                        text_len = NODE_INFO_TEXT_MAX
                    if len(body) < NODE_INFO_HDR_SIZE + text_len:
                        print("[GW] NodeInfoText short body:", "need", NODE_INFO_HDR_SIZE + text_len, "got", len(body))
                        return

                    text_bytes = body[NODE_INFO_HDR_SIZE:NODE_INFO_HDR_SIZE + text_len]
                    text = text_bytes.decode("ascii", errors="ignore")

                    uid_str = uid_bytes.hex()
                    self._remember_node_seen(mid=mid, ts=ts, uid=uid_str, mac=mac)
                    match_id = int(node_msg_id) if node_msg_id is not None else int(msg_id16)

                    pend = self._pending_pop_ack(mid, match_id)
                    srv_msg_id = (pend["srv_msg_id"] if pend else match_id)

                    resp = {
                        "cmd": "get_node_info_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "msg_id": srv_msg_id,
                        "node_msg_id": match_id,
                        "result": "success" if ok else "fail",
                        "err_code": int(err_code),
                        "ts": ts,
                        "data": {
                            "cfg_raw": text,
                            "lines": [ln for ln in text.splitlines() if ln.strip()],
                        },
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    
                    self.mqtt.publish_json(topic_cmd("get_node_info"), resp)
                    _verbose_print("[MQTT TX GET_NODE_INFO_ACK]", resp)
                    return
                # 1) AckBin (body[0] == 0x10)
                if len(body) >= ACK_BIN_SIZE and body[0] in (ACK_T, ACK_NODE_CFG_T):
                    try:
                        t_val, uid_bytes, msg_id32, ok, err_code = struct.unpack(ACK_BIN_FMT, body[:ACK_BIN_SIZE])
                    except struct.error as e:
                        print("[GW] AckBin unpack error:", e, "len=", len(payload))
                        _verbose_print("[GW] AckBin raw=", raw_hex)
                        self._comm_mark_error(
                            mid,
                            "ack_unpack_error",
                            ts=ts,
                            detail=str(e),
                            count_field="unpack_error_count",
                            mark_pending=True,
                        )
                        return
                        """ self.mqtt.publish_json(topic_raw, {
                            "cmd": "ack_unpack_error",
                            "gid": self.gwid,
                            "mid": mid,
                            "ts": ts,
                            "error": str(e),
                            "payload_hex": raw_hex,
                        })
                        return """

                    uid_str = uid_bytes.hex()
                    self._remember_node_seen(mid=mid, ts=ts, uid=uid_str, mac=mac)
                    result = "success" if ok else "fail"
                    
                    match_id = int(node_msg_id) if node_msg_id is not None else int(msg_id32 & 0xFFFF)

                    pend = self._pending_pop_ack(mid, match_id)
                    api = pend["api"] if pend else "unknown"

                    resp = {
                        "cmd": f"{api}_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "msg_id": (pend["srv_msg_id"] if pend else match_id),  # 서버 msg_id 우선
                        "node_msg_id": match_id,
                        "result": result,
                        "err_code": int(err_code),
                        "ts": ts,

                        # 디버깅용(원하면)
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    if pend and pend.get("meta"):
                        resp.update(pend["meta"])

                    self.mqtt.publish_json(topic_cmd(api), resp)
                    self.log.info("UL ack api=%s mid=%d node_msg_id=%s result=%s err=%d",
                    api, mid, match_id, result, int(err_code))
                    _verbose_print(f"[MQTT TX {api.upper()}_ACK]", resp)
                    return
                # status Bin 
                if len(body) >= STATUS_BIN_SIZE_V1 and body[0] == STATUS_T:
                    try:
                        status = unpack_status_bin(body)
                        if status is None:
                            raise struct.error("invalid status payload")
                    except struct.error as e:
                        print("[GW] StatusBin unpack error:", e, "len=", len(payload))
                        _verbose_print("[GW] StatusBin raw=", raw_hex)
                        self._comm_mark_error(
                            mid,
                            "status_unpack_error",
                            ts=ts,
                            detail=str(e),
                            count_field="unpack_error_count",
                            mark_pending=True,
                        )
                        self.mqtt.publish_json(topic_raw, {
                            "cmd": "status_unpack_error",
                            "gid": self.gwid,
                            "mid": mid,
                            "ts": ts,
                            "error": str(e),
                            "payload_hex": raw_hex,
                        })
                        return

                    uid_str = status["uid_bytes"].hex()
                    self._remember_node_seen(
                        mid=mid,
                        ts=ts,
                        uid=uid_str,
                        mac=mac,
                        voltage=status["volt"],
                        current=status["curr"],
                        temperature=status["temp"],
                        light_on=status["light_on"],
                    )

                    # match_id 규칙: transport header의 node_msg_id가 있으면 그걸 우선
                    match_id = int(node_msg_id) if node_msg_id is not None else int(status["msg_id32"] & 0xFFFF)

                    # pending에서 api를 뽑아오면 보통 "get_node_status"가 들어있게 됨
                    pend = self._pending_pop_ack(mid, match_id)   # 이름은 ack지만 "요청-응답 매칭" 용도로 재사용 가능
                    api = pend["api"] if pend else "get_node_status"

                    
                    """ if self.store is not None:
                        try:
                            self.store.upsert(
                                uid=uid_str,
                                mid=mid,
                                mac=mac,
                                ultrasonic=None,
                                voltage=volt,
                                current=curr,
                                temperature=temp,
                                ts=ts,
                            )
                        except Exception as e:
                            print("[NodeStore] update error(status_bin):", e) """
                    clean = _sanitize_node_measurements(
                        voltage=status["volt"],
                        current=status["curr"],
                        temperature=status["temp"],
                        light_on=status["light_on"],
                    )

                    resp = {
                        "cmd": api,   
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "msg_id": (pend["srv_msg_id"] if pend else match_id),
                        "node_msg_id": match_id,
                        "result": "success" if status["ok"] else "fail",
                        "err_code": int(status["err_code"]),
                        "ts": ts,
                        "data": {
                            "voltage": clean["voltage"],
                            "current": clean["current"],
                            "temperature": clean["temperature"],
                            "light_on": clean["light_on"],
                            "ok": int(status["ok"]),
                            "snap_count": status["snap_count"],
                        },

                        # 디버깅용(원하면)
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    resp.update(self._wisun_status_info(mid, now=ts))


                    self.mqtt.publish_json(topic_cmd("get_node_status"), resp)
                    _verbose_print("[MQTT TX GET_NODE_STATUS]", resp)
                    return

                # 2) SnapBin (body[0] == 0x01)
                if len(body) >= SNAP_BIN_SIZE and body[0] == T_SNAP:
                    try:
                        snap = unpack_snap_bin(body)
                        if snap is None:
                            raise struct.error("invalid snap payload")
                    except struct.error as e:
                        print("[GW] SnapBin unpack error:", e, "len=", len(payload))
                        _verbose_print("[GW] SnapBin raw=", raw_hex)
                        self._comm_mark_error(
                            mid,
                            "snap_unpack_error",
                            ts=ts,
                            detail=str(e),
                            count_field="unpack_error_count",
                            mark_pending=True,
                        )
                        self.mqtt.publish_json(topic_raw, {
                            "cmd": "uplink_unpack_error",
                            "gid": self.gwid,
                            "mid": mid,
                            "ts": ts,
                            "error": str(e),
                            "payload_hex": raw_hex,
                        })
                        return

                    uid_str = snap["uid_bytes"].hex()
                    fft1 = snap["fft_pairs"][0] if len(snap.get("fft_pairs", [])) >= 1 else (None, None)
                    fft2 = snap["fft_pairs"][1] if len(snap.get("fft_pairs", [])) >= 2 else (None, None)
                    if self._should_drop_duplicate_uplink("snap", mid, bytes(body)):
                        _verbose_print(
                            f"[GW DEDUPE] drop duplicate snap mid={mid} uid={uid_str} "
                            f"snap_count={snap.get('snap_count')} msg_id32={snap.get('msg_id32')}",
                            flush=True,
                        )
                        return
                    print(
                        f"[GW RAW_PARSE SNAP] mid={mid} uid={uid_str} "
                        f"layout={snap.get('layout')} "
                        f"body_hex={body.hex(' ')} "
                        f"tail_valid={snap.get('tail_valid')} "
                        f"light_on={snap.get('light_on')} "
                        f"fft_count={snap['fft_count']} "
                        f"f1={fft1[0]} a1={fft1[1]} "
                        f"f2={fft2[0]} a2={fft2[1]} "
                        f"snap_count={snap.get('snap_count')} msg_id32={snap.get('msg_id32')} "
                        f"ok={snap.get('ok')} err={snap.get('err_code')} "
                        f"ai_valid={snap.get('ai_valid')} ai_mse={snap.get('ai_mse')} "
                        f"ai_pred={snap.get('ai_pred')} flags={snap.get('flags')}",
                        flush=True,
                    )

                    use_snap_head = _fallback_snap_head_usable(snap)
                    fft = []
                    if use_snap_head and snap["fft_count"] >= 1:
                        f1, a1 = snap["fft_pairs"][0]
                        fft.append([f1, a1 if (snap.get("tail_valid") or snap.get("a1_valid")) else None])
                    if use_snap_head and snap.get("tail_valid") and snap["fft_count"] >= 2:
                        f2, a2 = snap["fft_pairs"][1]
                        fft.append([f2, a2])
                    if use_snap_head and _fft_missing_value(fft):
                        prev_rec = self._store_latest_by_mid(mid)
                        fallback_measure = self._measurement_fallback(mid, prev_rec)
                        merged_fft = _merge_fft_missing_values(fft, fallback_measure.get("fft"))
                        if merged_fft and merged_fft != _sanitize_fft(fft):
                            _verbose_print(
                                f"[GW SNAP FFT FALLBACK] mid={mid} uid={uid_str} "
                                f"raw_fft={fft} merged_fft={merged_fft}",
                                flush=True,
                            )
                            fft = merged_fft

                    self._remember_node_seen(
                        mid=mid,
                        ts=ts,
                        uid=uid_str,
                        mac=mac,
                        voltage=snap["volt"] if use_snap_head else None,
                        current=snap["curr"] if use_snap_head else None,
                        temperature=snap["temp"] if use_snap_head else None,
                        light_on=snap["light_on"] if use_snap_head else None,
                        fft=fft,
                        ai_valid=snap.get("ai_valid"),
                        ai_mse=snap.get("ai_mse"),
                        ai_pred=snap.get("ai_pred"),
                    )

                    with self._snap_cycle_lock:
                        self._snap_cycle_seen_mids.add(mid)

                    return

                if len(body) >= 19 and body[0] == CMD_SET_RTC_KST:
                    uid_str = body[1:13].hex()
                    rtc_msg_id = int.from_bytes(body[13:17], "little", signed=False)
                    rtc_ok = int(body[17])
                    rtc_err = int(body[18])

                    self._remember_node_seen(
                        mid=mid,
                        ts=ts,
                        uid=uid_str,
                        mac=mac,
                    )

                    _verbose_print(
                        f"[GW] RTC_KST uplink mid={mid} uid={uid_str} "
                        f"msg_id={rtc_msg_id} ok={rtc_ok} err={rtc_err}"
                    )
                    self.mqtt.publish_json(topic_raw, {
                        "cmd": "rtc_kst_uplink",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "ts": ts,
                        "msg_id": rtc_msg_id,
                        "ok": rtc_ok,
                        "err_code": rtc_err,
                        "payload_hex": raw_hex,
                    })
                    return

                # 3) 모르는 bytes → raw
                print("[GW] Unknown binary uplink, len=", len(payload))
                _verbose_print("[GW] Unknown binary raw=", raw_hex)
                self._comm_mark_error(
                    mid,
                    "unknown_uplink",
                    ts=ts,
                    detail="unknown binary uplink",
                    count_field="unknown_uplink_count",
                    mark_pending=True,
                )
                self.mqtt.publish_json(topic_raw, {
                    "cmd": "uplink_unknown_bin",
                    "gid": self.gwid,
                    "mid": mid,
                    "ts": ts,
                    "payload_hex": raw_hex,
                })
                return

            # 2) dict / list → 기존 JSON/dict 경로
            elif isinstance(payload, dict):
                msg = payload
            elif isinstance(payload, list):
                msg = payload
            else:
                print("[GW] Unknown payload type:", type(payload))
                self.mqtt.publish_json(topic_raw, {
                    "cmd": "uplink_unexpected_type",
                    "gid": self.gwid,
                    "mid": mid,
                    "ts": ts,
                    "py_type": str(type(payload)),
                })
                return

            # --- 여기부터는 "dict로 오는 binary 해석 결과" 처리 로직 ---
            _verbose_print("[GW-RX-DECODED]", msg)

            # 2-1) get_status 스타일 응답 (rqid 있는 dict)
            if isinstance(msg, dict) and "rqid" in msg:
                rqid = msg.get("rqid")
                ctx = self.pending_status.pop(rqid, None) if rqid is not None else None

                if self.store is not None:
                    try:
                        raw_uid = msg.get("uid")
                        if isinstance(raw_uid, (bytes, bytearray)):
                            uid_str = raw_uid.hex()
                        else:
                            uid_str = raw_uid

                        self._remember_node_seen(
                            mid=mid,
                            ts=ts,
                            uid=uid_str,
                            mac=msg.get("mac") or mac,
                            voltage=msg.get("v"),
                            current=msg.get("i"),
                            temperature=msg.get("tp", msg.get("t")),
                        )
                    except Exception as e:
                        print("[NodeStore] update error(get_status):", e)

                if ctx is not None:
                    resp = {
                        "cmd": "get_node_status",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": msg.get("uid"),
                        "msg_id": ctx.get("msg_id"),
                        "data": {
                            "ultrasonic": msg.get("ultrasonic"),
                            "voltage":    msg.get("v"),
                            "current":    msg.get("i"),
                            "temperature": msg.get("tp", msg.get("t")),
                        },
                        "result": "success",
                        "timestamp": ctx.get("timestamp_req"),
                    }
                    resp.update(self._wisun_status_info(mid, now=ts))
                    self.mqtt.publish_json(topic_cmd("get_node_status"), resp)
                    self.log.info("UL status mid=%d node_msg_id=%s result=%s err=%d",
                    mid, match_id, resp["result"], int(err_code))
                    _verbose_print("[MQTT TX GET_STATUS]", resp)
                    return

            # 2-2) dict 기반 snap (t == "snap")
            if isinstance(msg, dict) and msg.get("t") == "snap":
                try:
                    snap_fingerprint = json.dumps(msg, sort_keys=True, default=str)
                except Exception:
                    snap_fingerprint = repr(msg)
                if self._should_drop_duplicate_uplink("snap_dict", mid, snap_fingerprint):
                    _verbose_print(
                        f"[GW DEDUPE] drop duplicate dict snap mid={mid} "
                        f"uid={msg.get('uid')}",
                        flush=True,
                    )
                    return

                raw_uid = msg.get("uid")
                if isinstance(raw_uid, (bytes, bytearray)):
                    uid_str = raw_uid.hex()
                else:
                    uid_str = raw_uid

                self._remember_node_seen(
                    mid=mid,
                    ts=ts,
                    uid=uid_str,
                    mac=msg.get("mac") or mac,
                    voltage=msg.get("volt"),
                    current=msg.get("curr"),
                    temperature=msg.get("temp"),
                    fft=msg.get("fft"),
                )

                with self._snap_cycle_lock:
                    self._snap_cycle_seen_mids.add(mid)

                return

            # 2-3) 나머지 dict/list 는 디버그용으로 raw 토픽에만 올림
            self.mqtt.publish_json(topic_raw, {
                "cmd": "uplink",
                "gid": self.gwid,
                "mid": mid,
                "ts": ts,
                "data": msg,
            })
        except Exception as e:
            import traceback
            print("[GW] on_wisun_uplink EXC:", e, flush=True)
            traceback.print_exc()
    
