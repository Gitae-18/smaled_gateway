# node_scheduler.py
import json
import os
import time
import math
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, date, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
try:
    from astral.sun import sun
    from astral import LocationInfo
except Exception:
    sun = None
    LocationInfo = None
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

REGION_TABLE = {
    0: ("Default", 37.5665, 126.9780),   # 기본
    1: ("Seoul", 37.5665, 126.9780),
    2: ("Busan", 35.1796, 129.0756),
    3: ("Daegu", 35.8714, 128.6014),
    4: ("Incheon", 37.4563, 126.7052),
    5: ("Gwangju", 35.1595, 126.8526),
    6: ("Daejeon", 36.3504, 127.3845),
    7: ("Ulsan", 35.5384, 129.3114),
    8: ("Suwon", 37.2636, 127.0286),
    9: ("Gangwon", 37.8813, 127.7298),
    10: ("Jinju", 35.1802, 128.1076),
    11: ("Cheongju", 36.6424, 127.4890),
    12: ("Jeonju", 35.8242, 127.1480),
    13: ("Jeju", 33.4996, 126.5312),
}

def _load_kst():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Asia/Seoul")
        except Exception:
            pass
    return timezone(timedelta(hours=9), "KST")

KST = _load_kst()
CMD_SET_RTC_KST = 0x2B
RTC_SYNC_BCAST_MID = 0x0000
RTC_SYNC_PERIOD_MIN = 60
RTC_SYNC_STARTUP_DELAY_SEC = 0.0
GW_INFO_PATH = os.getenv("GW_INFO_PATH", "/home/pi/gw_info.json")
KASI_RISESET_URL = os.getenv(
    "KASI_RISESET_URL",
    "http://apis.data.go.kr/B090041/openapi/service/RiseSetInfoService/getLCRiseSetInfo",
)
KASI_SERVICE_KEY = os.getenv("KASI_SERVICE_KEY") or os.getenv("DATA_GO_KR_SERVICE_KEY") or ""
try:
    KASI_API_TIMEOUT_SEC = float(os.getenv("KASI_API_TIMEOUT_SEC", "5.0") or 5.0)
except (TypeError, ValueError):
    KASI_API_TIMEOUT_SEC = 5.0

def _minutes_since_midnight(value: datetime) -> int:
    local = value.astimezone(KST)
    return (local.hour * 60) + local.minute

def _read_gateway_gps(path: str = GW_INFO_PATH) -> Optional[tuple[float, float]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    gps = data.get("gps") if isinstance(data, dict) else None
    if not isinstance(gps, dict):
        return None

    try:
        lat = float(gps.get("lat"))
        lon = float(gps.get("lon"))
    except (TypeError, ValueError):
        return None

    if math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return lat, lon
    return None

def _hhmm_to_min(value) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ("-", "----"):
        return None
    s = "".join(ch for ch in s if ch.isdigit())
    if not s:
        return None
    s = s.zfill(4)
    try:
        hour = int(s[:-2])
        minute = int(s[-2:])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return (hour * 60) + minute
    return None

def _kasi_result_to_sun_min(item: Dict[str, Any]) -> Optional[Dict[str, int]]:
    if not isinstance(item, dict):
        return None
    keys = {
        "sunrise_min": ("sunrise", "sunriseTime"),
        "sunset_min": ("sunset", "sunsetTime"),
        "dawn_min": ("civilm", "civilMorning", "dawn"),
        "dusk_min": ("civile", "civilEvening", "dusk"),
    }
    result = {}
    for out_key, candidates in keys.items():
        value = None
        for candidate in candidates:
            if candidate in item:
                value = _hhmm_to_min(item.get(candidate))
                if value is not None:
                    break
        if value is None:
            return None
        result[out_key] = value
    return result

def _first_kasi_json_item(data) -> Optional[Dict[str, Any]]:
    try:
        item = data["response"]["body"]["items"]["item"]
    except (TypeError, KeyError):
        return None
    if isinstance(item, list):
        return item[0] if item else None
    return item if isinstance(item, dict) else None

def _first_kasi_xml_item(text: str) -> Optional[Dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    item = root.find(".//item")
    if item is None:
        return None
    return {child.tag: (child.text or "").strip() for child in list(item)}

def _build_kasi_url(day: date, lat: float, lon: float, service_key: str) -> str:
    params = {
        "locdate": day.strftime("%Y%m%d"),
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "dnYn": "N",
        "_type": "json",
    }
    query = urllib.parse.urlencode(params)
    encoded_key = service_key if "%" in service_key else urllib.parse.quote(service_key, safe="")
    return f"{KASI_RISESET_URL}?serviceKey={encoded_key}&{query}"

def _fetch_kasi_sun_min_payload(day: date, lat: float, lon: float) -> Optional[Dict[str, int]]:
    service_key = KASI_SERVICE_KEY.strip()
    if not service_key:
        return None

    url = _build_kasi_url(day, lat, lon, service_key)
    try:
        with urllib.request.urlopen(url, timeout=KASI_API_TIMEOUT_SEC) as resp:
            body = resp.read(65536)
            charset = resp.headers.get_content_charset() or "utf-8"
        text = body.decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[SCHED] KASI rise/set API failed, fallback to local calculation: {e}")
        return None

    try:
        data = json.loads(text)
        item = _first_kasi_json_item(data)
    except json.JSONDecodeError:
        item = _first_kasi_xml_item(text)

    result = _kasi_result_to_sun_min(item or {})
    if result is None:
        print("[SCHED] KASI rise/set API response parse failed, fallback to local calculation")
    return result

def _solar_event(day: date, lat: float, lon: float, zenith: float, sunrise: bool) -> datetime:
    n = day.timetuple().tm_yday
    lng_hour = lon / 15.0
    approx_hour = 6.0 if sunrise else 18.0
    t = n + ((approx_hour - lng_hour) / 24.0)

    mean_anomaly = (0.9856 * t) - 3.289
    true_long = (
        mean_anomaly
        + (1.916 * math.sin(math.radians(mean_anomaly)))
        + (0.020 * math.sin(math.radians(2 * mean_anomaly)))
        + 282.634
    ) % 360.0

    right_ascension = math.degrees(math.atan(0.91764 * math.tan(math.radians(true_long)))) % 360.0
    long_quadrant = math.floor(true_long / 90.0) * 90.0
    ra_quadrant = math.floor(right_ascension / 90.0) * 90.0
    right_ascension = (right_ascension + (long_quadrant - ra_quadrant)) / 15.0

    sin_dec = 0.39782 * math.sin(math.radians(true_long))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_hour = (
        math.cos(math.radians(zenith)) - (sin_dec * math.sin(math.radians(lat)))
    ) / (cos_dec * math.cos(math.radians(lat)))
    cos_hour = max(-1.0, min(1.0, cos_hour))

    hour_angle = math.degrees(math.acos(cos_hour))
    if sunrise:
        hour_angle = 360.0 - hour_angle
    hour_angle /= 15.0

    local_mean = hour_angle + right_ascension - (0.06571 * t) - 6.622
    utc_hour = (local_mean - lng_hour) % 24.0
    event_utc = datetime.combine(day, dt_time(0, 0), tzinfo=timezone.utc) + timedelta(hours=utc_hour)
    return event_utc.astimezone(KST)

def _sun_times_for_location(name: str, lat: float, lon: float, day: date) -> Dict[str, datetime]:
    if sun is not None and LocationInfo is not None:
        loc = LocationInfo(name=name, region="KR", timezone="Asia/Seoul", latitude=lat, longitude=lon)
        s = sun(loc.observer, date=day, tzinfo=loc.timezone)
        return {
            "sunrise": s["sunrise"],
            "sunset": s["sunset"],
            "dawn": s["dawn"],
            "dusk": s["dusk"],
        }

    return {
        "sunrise": _solar_event(day, lat, lon, 90.833, True),
        "sunset": _solar_event(day, lat, lon, 90.833, False),
        "dawn": _solar_event(day, lat, lon, 96.0, True),
        "dusk": _solar_event(day, lat, lon, 96.0, False),
    }

def _sun_times_for_region(region_code: int, day: date) -> Dict[str, datetime]:
    name, lat, lon = REGION_TABLE.get(region_code, REGION_TABLE[0])
    return _sun_times_for_location(name, lat, lon, day)

def _calc_sun_min_payload(region_code: int, day: date,
                          gps: Optional[tuple[float, float]] = None) -> Dict[str, int]:
    if gps is not None:
        lat, lon = gps
        kasi = _fetch_kasi_sun_min_payload(day, lat, lon)
        if kasi is not None:
            return kasi
        s = _sun_times_for_location("GPS", lat, lon, day)
    else:
        _name, lat, lon = REGION_TABLE.get(region_code, REGION_TABLE[0])
        kasi = _fetch_kasi_sun_min_payload(day, lat, lon)
        if kasi is not None:
            return kasi
        s = _sun_times_for_region(region_code, day)
    return {
        "sunrise_min": _minutes_since_midnight(s["sunrise"]),
        "sunset_min": _minutes_since_midnight(s["sunset"]),
        "dawn_min": _minutes_since_midnight(s["dawn"]),
        "dusk_min": _minutes_since_midnight(s["dusk"]),
    }

def build_rtc_kst_payload_with_sun_minutes(when: datetime, region_code: int = 0,
                                           gps: Optional[tuple[float, float]] = None) -> bytes:
    now = when.astimezone(KST)
    year = int(now.year)
    sun_min = _calc_sun_min_payload(region_code, now.date(), gps)
    return bytes([
        (year >> 8) & 0xFF,
        year & 0xFF,
        now.month & 0xFF,
        now.day & 0xFF,
        now.hour & 0xFF,
        now.minute & 0xFF,
        now.second & 0xFF,
        (sun_min["sunrise_min"] >> 8) & 0xFF,
        sun_min["sunrise_min"] & 0xFF,
        (sun_min["sunset_min"] >> 8) & 0xFF,
        sun_min["sunset_min"] & 0xFF,
        (sun_min["dawn_min"] >> 8) & 0xFF,
        sun_min["dawn_min"] & 0xFF,
        (sun_min["dusk_min"] >> 8) & 0xFF,
        sun_min["dusk_min"] & 0xFF,
    ])

class NodeScheduler:

    def __init__(self, wisun_client, config_path: str):

        self.wisun = wisun_client
        self.config_path = Path(config_path)
        self._stop_event = threading.Event()

        self.schedules: List[Dict[str, Any]] = []

        # 초기 로드
        self.load_from_file()
        if self.schedules:
            try:
                self._apply_all_schedules()
            except Exception as e:
                print(f"[SCHED] failed to apply saved schedules on boot: {e}")

        self._rtc_sync_thread = threading.Thread(
            target=self._rtc_sync_loop,
            name="node-rtc-sync",
            daemon=True,
        )
        self._rtc_sync_thread.start()

    def load_from_file(self) -> None:
        if not self.config_path.exists():
            # 파일 없으면 빈 스케줄
            self.schedules = []
            return

        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.schedules = data.get("schedules", [])
        except Exception as e:
            # 파싱 실패하면 일단 비워두고 로그만 찍어둔다.
            print(f"[SCHED] failed to load {self.config_path}: {e}")
            self.schedules = []

    def save_to_file(self, data: Dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        txt = json.dumps(data, ensure_ascii=False, indent=2)
        self.config_path.write_text(txt, encoding="utf-8")

    def update_from_payload(self, payload: bytes) -> None:
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception as e:
            print(f"[SCHED] invalid JSON payload: {e}")
            return

        # 파일 저장
        try:
            self.save_to_file(data)
        except Exception as e:
            print(f"[SCHED] failed to save config: {e}")

        # 메모리 갱신
        self.schedules = data.get("schedules", [])

        print(f"[SCHED] schedules updated: {len(self.schedules)} items")

        self._apply_all_schedules()

    def tick(self, now: Optional[datetime] = None) -> None:
        return

    def stop(self) -> None:
        self._stop_event.set()

    def get_snap_period_sec(self) -> Optional[int]:
        periods: List[int] = []
        for sch in self.schedules:
            snap = sch.get("snap", {}) or {}
            if not snap.get("enable", False):
                continue
            try:
                period_sec = int(snap.get("period_sec", 0) or 0)
            except (TypeError, ValueError):
                continue
            if period_sec > 0:
                periods.append(period_sec)
        if not periods:
            return None
        return min(periods)

    def _merge_schedules_for_apply(self) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}

        for sch in self.schedules:
            target = sch.get("target") or {"type": "broadcast"}
            target_type = target.get("type", "broadcast")
            target_id = target.get("id")
            target_key = f"{target_type}:{target_id}"

            if target_key not in merged:
                merged[target_key] = {
                    "name": sch.get("name", "noname"),
                    "target": dict(target),
                    "region_code": sch.get("region_code", 0),
                    "light": {},
                    "snap": {},
                }

            dst = merged[target_key]

            if "region_code" in sch and sch.get("region_code") is not None:
                dst["region_code"] = sch.get("region_code")

            light = sch.get("light")
            if isinstance(light, dict):
                dst["light"].update(light)

            snap = sch.get("snap")
            if isinstance(snap, dict):
                dst["snap"].update(snap)

            if sch.get("name"):
                dst["name"] = sch["name"]

        return list(merged.values())
    
    def _apply_all_schedules(self) -> None:
        for sch in self._merge_schedules_for_apply():
            name = sch.get("name", "noname")
            light = sch.get("light", {}) or {}
            snap = sch.get("snap", {}) or {}

            try:
                payload = self._build_node_cfg_payload(sch, light, snap)
            except Exception as e:
                print(f"[SCHED] failed to build node_cfg for {name}: {e}")
                continue

            try:
                self._send_node_cfg_broadcast(payload)
                print(f"[SCHED] applied node_cfg for {name}")
            except Exception as e:
                print(f"[SCHED] failed to send node_cfg for {name}: {e}")

    def _now_kst(self) -> datetime:
        return datetime.now(KST)

    def _build_rtc_sync_payload(self, when: Optional[datetime] = None) -> bytes:
        now = when.astimezone(KST) if when is not None else self._now_kst()
        region_code = self._region_code_for_target(RTC_SYNC_BCAST_MID)
        return build_rtc_kst_payload_with_sun_minutes(now, region_code, _read_gateway_gps())

    def _region_code_for_target(self, target_mid: int = RTC_SYNC_BCAST_MID) -> int:
        target_mid = int(target_mid or 0)

        for sch in self._merge_schedules_for_apply():
            target = sch.get("target") or {"type": "broadcast"}
            target_type = str(target.get("type", "broadcast")).lower()
            target_id = target.get("id")
            if target_mid == RTC_SYNC_BCAST_MID and target_type == "broadcast":
                return int(sch.get("region_code", 0) or 0)
            if target_mid != RTC_SYNC_BCAST_MID and target_id is not None:
                try:
                    if int(target_id) == target_mid:
                        return int(sch.get("region_code", 0) or 0)
                except (TypeError, ValueError):
                    continue

        return 0

    def build_rtc_sync_payload(self, when: Optional[datetime] = None,
                               target_mid: int = RTC_SYNC_BCAST_MID) -> bytes:
        now = when.astimezone(KST) if when is not None else self._now_kst()
        return build_rtc_kst_payload_with_sun_minutes(
            now,
            self._region_code_for_target(target_mid),
            _read_gateway_gps(),
        )

    def sync_rtc_kst_broadcast(self, when: Optional[datetime] = None) -> None:
        now = when.astimezone(KST) if when is not None else self._now_kst()
        payload = self._build_rtc_sync_payload(now)
        msg_id = int(now.timestamp()) & 0xFFFF
        print(f"[RTC SYNC TX] {now.strftime('%Y-%m-%d %H:%M:%S')}")

        self.wisun.send_cmd_bytes(
            target_mid=RTC_SYNC_BCAST_MID,
            cmd=CMD_SET_RTC_KST,
            msg_id=msg_id,
            flags=0x00,
            extra=payload,
        )
        print(f"[SCHED] broadcast rtc_kst {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    def _seconds_until_next_rtc_sync(self, now: Optional[datetime] = None) -> float:
        current = now.astimezone(KST) if now is not None else self._now_kst()
        target = current.replace(second=0, microsecond=0)
        next_minute = ((target.minute // RTC_SYNC_PERIOD_MIN) + 1) * RTC_SYNC_PERIOD_MIN
        if next_minute >= 60:
            target = (target + timedelta(hours=1)).replace(minute=0)
        else:
            target = target.replace(minute=next_minute)
        return max(1.0, (target - current).total_seconds())

    def _sleep_until_or_stop(self, seconds: float) -> bool:
        deadline = time.time() + max(0.0, seconds)
        while not self._stop_event.is_set():
            remain = deadline - time.time()
            if remain <= 0:
                return False
            time.sleep(min(1.0, remain))
        return True

    def _rtc_sync_loop(self) -> None:
        if not self._sleep_until_or_stop(RTC_SYNC_STARTUP_DELAY_SEC):
            try:
                self.sync_rtc_kst_broadcast()
            except Exception as e:
                print(f"[SCHED] failed to broadcast startup rtc sync: {e}")

        while not self._stop_event.is_set():
            wait_sec = self._seconds_until_next_rtc_sync()
            if self._sleep_until_or_stop(wait_sec):
                return
            try:
                self.sync_rtc_kst_broadcast()
            except Exception as e:
                print(f"[SCHED] failed to broadcast daily rtc sync: {e}")

    def calc_sun_times(self,region_code: int, date: datetime.date):
        return _sun_times_for_region(region_code, date)
             
    def compute_onoff_times(self,profile: dict, region_code: int):
        light = profile.get("light", {})
        mode = light.get("mode", "user_time")

        today = date.today()
        suninfo = self.calc_sun_times(region_code, today)

        # 반환값: (on_h, on_m, off_h, off_m, manual_duration)
        manual_duration = None

        if mode == "sunrise_sunset":
            # 일몰(sunset)에 offset 적용
            offset = int(light.get("sun_offset_min", 0))
            base = suninfo["sunset"]
            on_time = base + timedelta(minutes=offset)

            # 일출(sunrise)에 소등 offset 적용
            off_base = suninfo["sunrise"]
            off_time = off_base + timedelta(minutes=offset)

        elif mode == "civil_twilight":
            # 박명(dusk / dawn) 사용
            offset = int(light.get("sun_offset_min", 0))
            on_time = suninfo["dusk"] + timedelta(minutes=offset)
            off_time = suninfo["dawn"] + timedelta(minutes=offset)

        elif mode == "user_time":
            on_str = light.get("on_time", "19:00")
            off_str = light.get("off_time", "06:00")
            on_h, on_m = map(int, on_str.split(":"))
            off_h, off_m = map(int, off_str.split(":"))

            on_time = datetime.combine(today, dt_time(on_h, on_m))
            off_time = datetime.combine(today, dt_time(off_h, off_m))

        elif mode == "manual":
            # manual은 ON 즉시 + duration만큼 유지
            manual_duration = light.get("manual_duration_min", 10)
            now = datetime.now()
            on_time = now
            off_time = now + timedelta(minutes=manual_duration)

        else:
            # 안전 default
            on_time = datetime.combine(today, dt_time(19, 0))
            off_time = datetime.combine(today, dt_time(6, 0))

        return {
            "on_h": on_time.hour,
            "on_m": on_time.minute,
            "off_h": off_time.hour,
            "off_m": off_time.minute,
            "manual_duration": manual_duration,
        }
    
    def _build_node_cfg_payload(self,profile,
                            light: Dict[str, Any],
                            snap: Dict[str, Any]) -> bytes:
        
        region_code = profile.get("region_code", 0)
        # 1) mode
        mode_str = light.get("mode", "user_time")  # 기본 user_time
        mode_map = {
            "sunrise_sunset": 0,
            "civil_twilight": 1,
            "user_time": 2,
            "manual": 3,
        }
        mode = mode_map.get(mode_str, 2)

        # 2) on/off 시간
        """ on_h = on_m = off_h = off_m = 0
        if mode == 2:  # user_time
            try:
                on_h, on_m = map(int, light.get("on_time", "19:00").split(":"))
                off_h, off_m = map(int, light.get("off_time", "06:00").split(":"))
            except Exception:
                raise ValueError(f"invalid on/off time: {light!r}") """
        times = self.compute_onoff_times(profile, region_code)

        on_h = times["on_h"]
        on_m = times["on_m"]
        off_h = times["off_h"]
        off_m = times["off_m"]
        manual_duration = times["manual_duration"]

        # 3) manual_duration_min
        manual_dur_min = 0
        if mode == 3:
            manual_dur_min = int(light.get("manual_duration_min", 10))
            if manual_dur_min < 0:
                manual_dur_min = 0
            if manual_dur_min > 255:
                manual_dur_min = 255

        # 4) snap 설정
        snap_enable = 1 if snap.get("enable", False) else 0
        period_sec = int(snap.get("period_sec", 0))
        if period_sec <= 0 or not snap_enable:
            snap_period_min = 0
        else:            
            snap_period_min = max(1, min(255, period_sec // 60))

        data = bytearray(9)
        data[0] = 1  # node firmware expects version first
        data[1] = mode & 0xFF
        data[2] = on_h & 0xFF
        data[3] = on_m & 0xFF
        data[4] = off_h & 0xFF
        data[5] = off_m & 0xFF
        data[6] = manual_dur_min & 0xFF
        data[7] = snap_enable & 0xFF
        data[8] = snap_period_min & 0xFF

        return bytes(data)
    
    def _send_node_cfg_broadcast(self, payload: bytes) -> None:

        CMD_NODE_CFG = 0x20
        FLAGS = 0x00
        BCAST_MID = 0x0000 

        self.wisun.send_cmd_bytes(
            target_mid=BCAST_MID,
            cmd=CMD_NODE_CFG,
            msg_id=0,
            flags=0x00,
            extra=payload,
        )
