# gps_auto_setup_fixed.py
import time, serial

AT_PORT   = "/dev/ttyUSB2"
BAUD      = 115200
TIMEOUT_S = 2

GPS_FIX_MODE    = 2                 # Standalone
GPS_FIX_TIMEOUT = 300               # sec
GPS_SAT_MASK    = 4294967280        # ★ 위성 마스크(정확도 아님!)
NMEA_MASK       = "C01BDFFF"

POLL_INTERVAL_S = 10
FIRST_WAIT_S    = 10                # ★ GPSFIX 직후 첫 조회까지 대기
POST_END_WAIT_S = 1.5               # ★ GPSEND 직후 안정화 대기

def send(ser, cmd, wait=0.5):
    ser.write((cmd + "\r").encode())
    time.sleep(wait)
    resp = ser.read_all().decode(errors="ignore")
    print(f">>> {cmd}\n{resp.strip()}\n")
    return resp

def main():
    ser = serial.Serial(AT_PORT, BAUD, timeout=TIMEOUT_S)
    try:
        # NMEA 구성
        send(ser, f"AT!GPSNMEASENTENCE={NMEA_MASK}")
        send(ser, "AT!GPSNMEACONFIG=1")

        # 기존 세션 종료 후 약간 대기
        send(ser, "AT!GPSEND")
        time.sleep(POST_END_WAIT_S)

        # 버퍼 비우기
        ser.reset_input_buffer()

        # GPSFIX 시작
        resp = send(ser, f"AT!GPSFIX={GPS_FIX_MODE},{GPS_FIX_TIMEOUT},{GPS_SAT_MASK}")
        if "ERROR" in resp:
            print("[ERR] GPSFIX 구문/인자 오류. 3번째 인자가 위성 마스크인지 확인하세요.")
            return

        # 초기 대기 후 상태 폴링
        print(f"Fix 시도 중… {FIRST_WAIT_S}s 대기")
        time.sleep(FIRST_WAIT_S)

        waited = FIRST_WAIT_S
        while waited <= GPS_FIX_TIMEOUT:
            r = send(ser, "AT!GPSSTATUS?")
            if "SUCCESS" in r:
                print("[OK] GPS Fix 성공")
                send(ser, "AT!GPSLOC?")
                break
            if "FAIL" in r:
                print("[FAIL] 세션 실패 — 환경/안테나/타임아웃 재점검")
                break
            time.sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S
        else:
            print("[TIMEOUT] 제한 시간 내 Fix 미완료")

    finally:
        ser.close()

if __name__ == "__main__":
    main()
