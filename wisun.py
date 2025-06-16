import serial
import struct
import cbor2

# UART 포트 설정
uart = serial.Serial('/dev/ttyAMA2', baudrate=9600, timeout=1)

# Checksum 계산 (XOR 방식)
def calc_checksum(data: bytes) -> int:
    checksum = 0x00
    for b in data:
        checksum ^= b
    return checksum

# AT 명령어 처리
def send_at_command(cmd: str):
    print(f"[AT] {cmd}")
    uart.write((cmd + '\r\n').encode())

# Wi-SUN 패킷 생성 및 전송
def send_wisun_packet(mid: int, payload: bytes):
    header = bytes([0xA0, 0xA0])
    length = 2 + len(payload) + 1  # MID(2) + payload + checksum
    length_bytes = struct.pack('>H', length)
    mid_bytes = struct.pack('>H', mid)

    packet_body = mid_bytes + payload
    checksum = calc_checksum(packet_body)
    packet = header + length_bytes + packet_body + bytes([checksum])

    print(f"[Wi-SUN] TX → MID: {mid:04X}, LEN: {length}, CHK: {checksum:02X}")
    uart.write(packet)

# CBOR 명령 수신 및 처리
def handle_command_from_server(cbor_bytes: bytes):
    try:
        decoded = cbor2.loads(cbor_bytes)
        print(f"수신된 CBOR 명령: {decoded}")

        cmd = decoded.get("cmd")
        payload = decoded.get("payload", {})
        target = decoded.get("target", "self")
        mid = decoded.get("mid", 0x0001)

        if cmd.startswith("getid_") and target == "self":
            # ex: getid_gid
            print("게이트웨이용 AT 커맨드 실행")
            send_at_command("AT+GID?")
        
        elif cmd.startswith("setid_") and target == "node":
            # ex: setid_key
            key = payload.get("key", "default")
            payload_bytes = key.encode()
            print(f"노드에 패킷 전송 MID={mid}, KEY={key}")
            send_wisun_packet(mid, payload_bytes)

        elif cmd == "set_interval" and target == "gateway":
            interval = payload.get("interval")
            print(f"게이트웨이 전송 주기 설정: {interval}초")
            # 실제 처리 로직 여기에 추가

        else:
            print(f"알 수 없는 명령: {cmd}")

    except Exception as e:
        print(f"CBOR 파싱 실패: {e}")

def main():
    # 테스트용 CBOR (직접 바이트 생성)
    example_data = {
        "cmd": "set_interval",
        "mid": 1,
        "target": "gateway",
        "payload": {
            "interval": 60
        }
    }
    cbor_bytes = cbor2.dumps(example_data)

    # 👇 이 부분에서 함수 호출
    handle_command_from_server(cbor_bytes)

# 예시 CBOR 호출
if __name__ == "__main__":
    main()
