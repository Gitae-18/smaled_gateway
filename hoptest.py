import serial
import time
import threading

exit_flag = False
def send_broadcast_packet_loop(uart):
    while not exit_flag:
        send_broadcast_packet(uart)
        time.sleep(5)

def send_broadcast_packet(uart):
    data_field = b"HELLO"
    data_length = len(data_field)
    total_length = data_length + 2

    target_mid = 0x0000
    packet = bytearray(6 + data_length + 2)

    packet[0] = 0x02
    packet[1] = 0xAA
    packet[2] = 0xAB
    packet[3] = total_length
    packet[4] = target_mid & 0xFF
    packet[5] = (target_mid >> 8) & 0xFF

    packet[6:6 + data_length] = data_field

    checksum = 0
    for i in range(1, 6 + data_length):
        checksum ^= packet[i]
    packet[6 + data_length] = checksum
    packet[7 + data_length] = 0x03

    # 패킷 출력 및 전송
    print("[Python 전송 HEX]:", ' '.join(f'{b:02X}' for b in packet))
    print("[길이]:", len(packet))
    uart.write(packet)
    uart.flush()

def send_at_command(uart, cmd):
    full_cmd = cmd + "\r\n"
    uart.write(full_cmd.encode())
    print(f"[AT 명령 전송] {repr(full_cmd)}")

    time.sleep(0.3)
    response = uart.read_all()
    print("[AT 응답]", response.decode(errors='ignore').strip(), "\n")

def main():
    global exit_flag
    try:
        with serial.Serial('/dev/ttyAMA2', 9600, timeout=1) as uart:
            # 브로드캐스트 전송 스레드 시작
            thread = threading.Thread(target=send_broadcast_packet_loop, args=(uart,))
            thread.start()

            print("명령 입력 대기 중... ('exit' 입력 시 종료)")
            while True:
                user_input = input(">> ").strip()
                if user_input.lower() == "exit":
                    exit_flag = True
                    break
                elif user_input.startswith("AT"):
                    send_at_command(uart, user_input)
                else:
                    print("※ AT로 시작하지 않아 무시됨.")

            thread.join()
            print("종료 완료.")

    except serial.SerialException as e:
        print("UART 열기 실패:", e)

# 실행
if __name__ == "__main__":
    main()