import serial, time, sys

AT_PORT = "/dev/ttyUSB2"   # 확인된 AT 포트
BAUD = 115200
ser = serial.Serial("/dev/ttyUSB2", 115200, timeout=1)
def at_send(ser, cmd, wait=0.4):
    ser.write((cmd + "\r").encode())
    time.sleep(wait)
    resp = ser.read_all().decode(errors="ignore")
    print(f">>> {cmd}\n{resp}\n")
    return resp

def check_lte(ser):
    # 종합 상태 (RAT/PS attach/RSRP/RSRQ/SINR 등)
    # 응답 내 'PS state: Attached' 확인
    resp = at_send(ser, "AT!GSTATUS?")
    attached = ("PS state: Attached" in resp) or ("PS state:Attached" in resp)
    return attached, resp

def gps_config_nmea(ser, enable_mask_hex="001F"):
    # 1) NMEA 출력 켜기
    at_send(ser, "AT!GPSNMEACONFIG=1")  # enable
    # 2) 필요한 문장만 선택 (GGA/RMC/GSA/GSV/VTG = 0x001F)
    at_send(ser, f"AT!GPSNMEASENTENCE={enable_mask_hex}")

def gps_fix_start(ser, fix_type=2, max_time=120, max_dist=4294967280):
    # GPS Fix 시작
    at_send(ser, f"AT!GPSFIX={fix_type},{max_time},{max_dist}")

def gps_status_poll(ser, total_s=120, step_s=10):
    # ACTIVE/SUCCESS/FAIL 상태 폴링
    loops = total_s // step_s
    for i in range(loops):
        print(f"[{i*step_s}s] Fix 상태 확인...")
        time.sleep(step_s)
        resp = at_send(ser, "AT!GPSSTATUS?")
        if "SUCCESS" in resp:
            return "SUCCESS", resp
        if "FAIL" in resp:
            return "FAIL", resp
    return "TIMEOUT", ""

def at(cmd):
    ser.write((cmd + "\r").encode())
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            break
        print("[AT]", line)
        if line == "OK" or line.startswith("ERROR"):
            break

def start_gps_and_read_nmea(duration_s=120):
    ser = serial.Serial(AT_PORT, BAUD, timeout=1)
    try:        
        # at_send(ser, 'AT!ENTERCND="A710"')

        # LTE 상태 먼저 체크(전용 SUPL 안쓸거라도, 망 붙었는지 확인 지표)
        attached, gstatus = check_lte(ser)
        print(f"LTE Attached: {attached}")

        # NMEA 설정
        gps_config_nmea(ser, enable_mask_hex="001F")

        # Fix 시작
        gps_fix_start(ser, fix_type=2, max_time=duration_s, max_dist=4294967280)
        
        start = time.time()
        buf = []
        while time.time() - start < duration_s:
            line = ser.readline().decode(errors="ignore").strip()
            if line.startswith("$G"):  # NMEA
                print(line)
                buf.append(line)
            # 가끔 상태도 찍기
            if int(time.time()-start) % 10 == 0:
                pass

        # 마지막 상태 확인
        status, last = gps_status_poll(ser, total_s=10, step_s=5)
        print("GPS FINAL:", status)
        return {"lte_attached": attached, "gstatus": gstatus, "gps_status": status, "nmea": buf}
    finally:
        ser.close()

if __name__ == "__main__":
    #res = start_gps_and_read_nmea(duration_s=120)
    #at_send(ser, "AT+CFUN=0")
    #time.sleep(3)
    #at_send(ser,"AT+CFUN=1")
    #time.sleep(5)  
    #at_send(ser,"AT+COPS=0")
    #at_send(ser,"AT+CEREG?")
    #at_send(ser,"AT+CGATT=1")
    at_send(ser, 'AT!ENTERCND="A710"')
    #at_send(ser, 'AT!GNSSCONFIG=1,1,1,1,1')
    #at_send(ser, 'AT!GPSNMEACONFIG=1')
    #at_send(ser, 'AT!GPSNMEASENTENCE=000F')
    #at_send(ser, 'AT!RESET')
    gps_fix_start(ser,fix_type=2, max_time=120, max_dist=4294967280)
    for i in range(12):
        time.sleep(5)
        at_send(ser, 'AT!GPSSTATUS?')
    #at('AT!GPSNMEACONFIG?')
    #at('AT!GPSNMEASENTENCE?')

    # 2) Tracking 세션 시작 (여러 번 fix 시도하고 계속 NMEA 뿌리는 모드)
    #    fixType=1(Standalone), maxTime=60s, maxDist=No preference,
    #    fixCount=1000(사실상 계속), fixRate=1s 주기
    #at('AT!GPSTRACK=1,60,4294967280,1000,1')

    #print("=== NMEA 읽기 시작 ===")
    #while True:
     #   line = ser.readline().decode(errors="ignore").strip()
     #   if not line:
     #       continue
     #   if line.startswith("$"):
     #       print("[NMEA]", line)
    #time.sleep(10)
    #at_send(ser, 'AT!GPSSTATUS?')
    #at_send(ser, "AT+CIMI")
    #at_send(ser, "AT+ICCID?")
    #at_send(ser, "AT+COPS=0")
    #at_send(ser, "AT+CEREG?")
    #at_send(ser, "AT+CPIN?")
    #at_send(ser, "AT+CCID")
    #at_send(ser, "AT+COPS?")
    #at_send(ser, "AT+CEREG?")
    #at_send(ser, "AT+CSQ")
    #at_send(ser, "AT!SELRAT?")
    #at_send(ser, "AT!BAND?")
    #at_send(ser, "AT+CGPADDR=1")
    #at_send(ser, "AT+CEER")
    #at_send(ser, "AT+CGSN")
    #at_send(ser, "AT+CGDCONT?")
    #at_send(ser, "AT!GSTATUS?")
    #at_send(ser, "AT!USBCOMP=?")
    #at_send(ser, "AT!USBCOMP?")
    #at_send(ser, "AT+QGPSLOC?")
    #at_send(ser, "AT+CGDCONT=2")
    #at_send(ser, "AT+CGDCONT=3")
    #at_send(ser, "AT+CGDCONT=4")
