import serial
import time
import json
import os
import threading

# ====== 설정값 ======
AT_PORT      = "/dev/ttyUSB2"
AT_BAUD      = 115200
GW_INFO_PATH = "/home/pi/gw_info.json"
GPS_DEBUG_AT = False 
# GPSTRACK 파라미터
GPSTRACK_FIX_TYPE  = 1            # 1: Standalone
GPSTRACK_MAX_TIME  = 120          # 각 fix 시도당 최대 대기 시간(sec)
GPSTRACK_MAX_DIST  = 4294967280   # 정확도 제한 없음
GPSTRACK_FIX_COUNT = 1000         # 사실상 계속
GPSTRACK_FIX_RATE  = 60           # 60초마다 fix 시도

# 내부 상태
_gps_started    = False
_gps_start_lock = threading.Lock()
_first_fix_logged   = False

# ====== 유틸 함수들 ======

def at_send(ser: serial.Serial, cmd: str, wait: float = 0.5, verbose: bool = False) -> str:
    """AT 명령 전송 + 응답 문자열 리턴 (간단 버전)"""
    ser.write((cmd + "\r").encode())
    time.sleep(wait)
    resp = ser.read_all().decode(errors="ignore")
    
    if GPS_DEBUG_AT or verbose:
        print(f"[GPS][AT] >>> {cmd}")
        print(resp)
        print("-" * 40)

    return resp

def save_gps_to_file(lat: float, lon: float) -> None:
    data = {
        "gps": {
            "lat": lat,
            "lon": lon,
            "ts": int(time.time())
        }
    }
    tmp = GW_INFO_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, GW_INFO_PATH)
    #print(f"[GPS] saved lat={lat:.6f}, lon={lon:.6f} -> {GW_INFO_PATH}")


def _parse_angle_line(line: str) -> float | None:    
    try:
        _, right = line.split(":", 1)
    except ValueError:
        return None

    tokens = right.strip().split()
    # 기대 형태: [deg, 'Deg', min, 'Min', sec, 'Sec', dir, '(0x...)']
    if len(tokens) < 7:
        return None

    try:
        deg = float(tokens[0])
        minutes = float(tokens[2])
        seconds = float(tokens[4])
        direction = tokens[6]
    except (ValueError, IndexError):
        return None

    dec = deg + minutes / 60.0 + seconds / 3600.0
    if direction in ("S", "W"):
        dec = -dec
    return dec


def parse_gpsloc_response(resp: str) -> tuple[float, float] | None:
    """
    AT!GPSLOC? 응답 문자열에서 (lat, lon) 십진도 튜플 추출.    
    """
    if "Unknown" in resp or "Not Available" in resp:
        return None

    lat = None
    lon = None

    for line in resp.splitlines():
        line = line.strip()
        if line.startswith("Lat:"):
            lat = _parse_angle_line(line)
        elif line.startswith("Lon:"):
            lon = _parse_angle_line(line)

    if lat is not None and lon is not None:
        return (lat, lon)
    return None


# ====== GNSS 세션 시작 (GPSTRACK) ======

def init_gnss_via_at():    
    global _gps_started
    with _gps_start_lock:
        if _gps_started:
            return

        print("[GPS] init_gnss_via_at() start")
        with serial.Serial(AT_PORT, AT_BAUD, timeout=1) as ser:
            # 관리자 모드
            at_send(ser, 'AT!ENTERCND="A710"')

            # 필요시 GNSS/NMEA 설정도 가능하지만, GPSLOC만 쓸 거라 필수는 아님
            # at_send(ser, 'AT!GNSSCONFIG=1,1,1,1,1')
            
            cmd = f"AT!GPSTRACK={GPSTRACK_FIX_TYPE},{GPSTRACK_MAX_TIME}," \
                  f"{GPSTRACK_MAX_DIST},{GPSTRACK_FIX_COUNT},{GPSTRACK_FIX_RATE}"
            at_send(ser, cmd, wait=0.5)
            
            at_send(ser, "AT!GPSSTATUS?", wait=0.5)

        _gps_started = True
        print("[GPS] init_gnss_via_at() done")


# ====== 위치 읽기 (GPSLOC) ======

def get_last_fix_location() -> tuple[float, float] | None:
    """
    AT!GPSLOC? 를 한 번 보내서 마지막 Fix 좌표를 읽어온다.
    성공 시 (lat, lon) 십진도 튜플, 실패 시 None.
    """    
    if not _gps_started:
        init_gnss_via_at()

    try:
        with serial.Serial(AT_PORT, AT_BAUD, timeout=1) as ser:
            resp = at_send(ser, "AT!GPSLOC?", wait=1.0)
    except Exception as e:
        print("[GPS] get_last_fix_location() error:", e)
        return None

    loc = parse_gpsloc_response(resp)
    global _first_fix_logged
    """ if loc:
        lat, lon = loc
        print(f"[GPS] GPSLOC lat={lat:.6f}, lon={lon:.6f}")
    else:
        print("[GPS] GPSLOC: no valid location (no fix yet)") """
    if loc:
        lat, lon = loc        
        if not _first_fix_logged:
            print(f"[GPS] first fix lat={lat:.6f}, lon={lon:.6f}")
            _first_fix_logged = True
    return loc


# ====== 백그라운드 루프 ======

def gps_reader_loop(poll_interval: int = 60):
    """
    주기적으로 AT!GPSLOC? 를 호출해서
    유효한 좌표를 gw_info.json 에 반영하는 루프.
    """
    init_gnss_via_at()

    while True:
        loc = get_last_fix_location()
        if loc:
            lat, lon = loc
            save_gps_to_file(lat, lon)
        time.sleep(poll_interval)


def start_gps_thread(poll_interval: int = 60):
    """
    gateway.py 등에서 호출:
        from gps_reader import start_gps_thread
        start_gps_thread()
    """
    t = threading.Thread(target=gps_reader_loop, args=(poll_interval,), daemon=True)
    t.start()
    print("[GPS] gps_reader thread started (interval=%ds)" % poll_interval)


if __name__ == "__main__":
    gps_reader_loop(poll_interval=30)
