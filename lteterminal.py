import serial
import time

port = "/dev/ttyUSB2"  # 확인된 AT 포트
ser = serial.Serial(port, 115200, timeout=2)

def send_cmd(cmd):
    full_cmd = f"{cmd}\r".encode()
    ser.write(full_cmd)
    time.sleep(0.5)
    resp = ser.read_all().decode(errors='ignore')
    print(f">>> {cmd}\n{resp}\n")


send_cmd("AT!CUSTOM=\"GPSENABLE\",1")
ser.close()
