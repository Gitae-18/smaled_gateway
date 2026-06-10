# node_scheduler.py
import json
import time
import threading
from datetime import datetime, date, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from astral.sun import sun
from astral import LocationInfo
from zoneinfo import ZoneInfo

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

KST = ZoneInfo("Asia/Seoul")
CMD_SET_RTC_KST = 0x2B
RTC_SYNC_BCAST_MID = 0x0000
RTC_SYNC_PERIOD_MIN = 60
RTC_SYNC_STARTUP_DELAY_SEC = 0.0

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
        name, lat, lon = REGION_TABLE.get(region_code, REGION_TABLE[0])
        loc = LocationInfo(name=name, region="KR", timezone="Asia/Seoul", latitude=lat, longitude=lon)
        s = sun(loc.observer, date=date, tzinfo=loc.timezone)

        return {
            "sunrise": s["sunrise"],
            "sunset": s["sunset"],
            "dawn": s["dawn"],          # civil twilight 시작
            "dusk": s["dusk"],          # civil twilight 종료
        }
             
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
