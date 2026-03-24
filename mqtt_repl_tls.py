# mqtt_repl_tls.py
import time
import json
from mqtt_client import MqttBridge

BROKER     = "mqtt.hananet.co.kr"   # 서버 주소
BASE_TOPIC = "wisun"
GID        = "gw001"                # 게이트웨이 ID (gw001 인증서 기준)
CERT_DIR   = "/etc/mosquitto/certs" # ca.crt, gw001.crt, gw001.key 위치
TLS_PORT   = 8883

# 구독 콜백: MqttBridge에 맞춰 payload, topic 받는 형태라고 가정
def on_cmd(payload, topic):
    print(f"[RX] {topic} -> {payload}")

def main():
    # TLS 포트로 바로 연결
    mqtt = MqttBridge(host=BROKER, port=TLS_PORT, base=BASE_TOPIC, client_id=GID)

    # TLS 1.2 이상, gw001 인증서 사용
    mqtt.configure_tls_min12_from(
        cert_dir=CERT_DIR,
        gateway_id=GID,
        insecure=False,   # 필요하면 True로 풀어서 테스트 가능
    )

    try:
        mqtt.add_cmd_listener(on_cmd, topic_filter=f"{BASE_TOPIC}/gw/cmd/#")
    except AttributeError:
        # 만약 add_cmd_listener 없으면, mqtt._client.on_message 직접 써도 됨
        def _on_msg(client, userdata, msg):
            print(f"[RAW RX] {msg.topic} -> {msg.payload}")
        mqtt._client.on_message = _on_msg
        mqtt._client.subscribe(f"{BASE_TOPIC}/#")

    mqtt.connect()
    mqtt.loop_start()
    print(f"[INFO] connected to {BROKER}:{TLS_PORT} as {GID}")
    print(f"[INFO] subscribing: {BASE_TOPIC}/gw/cmd/#")
    print("------ MQTT REPL 시작 ------")
    print("형식:")
    print("  <topic> <payload>  → 해당 topic으로 publish")
    print("  그냥 텍스트       → 기본 토픽 wisun/debug 로 publish")
    print("  quit / exit        → 종료")
    print("----------------------------")

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break

            if not line:
                continue
            if line in ("quit", "exit", "q"):
                break

            # topic + payload 로 나뉘었는지 확인
            parts = line.split(maxsplit=1)
            if len(parts) == 1:
                topic = f"{BASE_TOPIC}/debug"
                payload_text = parts[0]
            else:
                topic, payload_text = parts

            # payload가 JSON이면 JSON으로 보내고, 아니면 그냥 텍스트
            try:
                obj = json.loads(payload_text)
                mqtt.publish_json(topic, obj)
                print(f"[TX JSON] {topic} <- {obj}")
            except json.JSONDecodeError:
                # 텍스트 그대로
                mqtt._client.publish(topic, payload_text)
                print(f"[TX TEXT] {topic} <- {payload_text}")

    finally:
        print("[INFO] disconnecting...")
        mqtt.disconnect()
        time.sleep(0.5)

if __name__ == "__main__":
    main()
