import serial
import threading
import sys
import time

PORT = "/dev/ttyAMA2"  # 실제 사용하는 포트로 변경
BAUD = 9600            # 모듈 설정 속도에 맞게

# TODO: 여기 HEX는 실제 사용 중인 SNAP 요청 프레임으로 교체해야 함
# 예: [UART TX FRAME] 로그에서 복붙한 값
# "02 aa ab 1e 00 00 ..." 이런 식으로
SNAP_REQ_HEX = "02 aa ab 1e 00 00 a5 61 6b 00 61 74 64 73 6e 61 70 63 63 6d 64 13 66 6d 73 67 5f 69 64 00 64 72 71 69 64 00 f0 03"
SNAP_REQ = bytes.fromhex(SNAP_REQ_HEX)


def hexdump(prefix: str, data: bytes):
    if not data:
        return
    hex_str = " ".join(f"{b:02X}" for b in data)
    print(f"{prefix} {hex_str}")


def rx_loop(ser: serial.Serial):
    """UART 수신 쓰레드: 들어오는 모든 바이트를 HEX로 출력"""
    try:
        while ser.is_open:
            data = ser.read(256)
            if data:
                hexdump("[RX]", data)
            else:
                # 너무 바쁘지 않게 약간 쉼
                time.sleep(0.01)
    except Exception as e:
        print(f"[RX] error: {e!r}")


def main():
    print(f"[INFO] Open serial {PORT} @ {BAUD}")
    ser = serial.Serial(PORT, baudrate=BAUD, timeout=0.05)

    # RX 쓰레드 시작
    t = threading.Thread(target=rx_loop, args=(ser,), daemon=True)
    t.start()

    print("=== Wi-SUN UART 테스트 ===")
    print(" - AT 커맨드는 그대로 입력 후 Enter (예: AT, ATI, ATS400?)")
    print(" - '1' 입력 후 Enter → SNAP 요청 프레임 전송")
    print(" - 'q' 입력 후 Enter → 종료")
    print("==========================")

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break

            if line == "":
                continue

            if line.lower() == "q":
                print("[INFO] quit")
                break

            elif line == "1":
                # SNAP 요청 프레임 전송
                hexdump("[TX SNAP]", SNAP_REQ)
                ser.write(SNAP_REQ)
                ser.flush()

            else:
                # 일반 문자열 → AT 커맨드 전송 (CRLF 붙이기)
                cmd = line
                if not cmd.endswith("\r\n"):
                    cmd = cmd + "\r\n"
                print(f"[TX STR] {repr(cmd)}")
                ser.write(cmd.encode("ascii", errors="ignore"))
                ser.flush()

    finally:
        ser.close()
        print("[INFO] serial closed")


if __name__ == "__main__":
    main()
