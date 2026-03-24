# send_once_tls.py
import time
from mqtt_client import MqttBridge
import json

BROKER     = "mqtt.hananet.co.kr"   # 서버 주소
BASE_TOPIC = "wisun"
GID        = "gw001"          # 게이트웨이 ID (generate_token에 쓴 ID와 같아야 함)
CERT_DIR = "/etc/mosquitto/certs" 
TLS_PORT   = 8883

uplink_topic = f"gw/{GID}/status" 

def on_cmd(payload, topic):
    print(f"[RX] {topic} -> {payload}")

def main():
    # 처음부터 TLS 포트로 설정
    mqtt = MqttBridge(host=BROKER, port=TLS_PORT, base=BASE_TOPIC, client_id=GID)
    mqtt.add_handler(on_cmd)

    # TLS 1.2 이상 설정 (프로비저닝으로 받은 인증서 사용)
    mqtt.configure_tls_min12_from(cert_dir=CERT_DIR, gid=GID, insecure=False)

    # TLS로 바로 접속
    mqtt.connect()
    mqtt.loop_start()
    time.sleep(0.5)

    # 전송 (CBOR/JSON)
    payload = {
        "device_type": "gateway",
        "gateway_id": GID,
        "msg_type": "status",
        "msg_id": "msg001",
        "v": 12.1,
        "i": 0.33,
        "t": 27.4,
        "ts": int(time.time()),
    }

    mqtt.publish_json(uplink_topic, payload)

    print(f"[TX] sent CBOR to {uplink_topic}")    
    print("[TX] sent CBOR to wisun/uplink and JSON to wisun/debug")

    time.sleep(1)
    mqtt.disconnect()

if __name__ == "__main__":
    main()
