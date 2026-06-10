# gateway_main.py
import time
import json
from mqtt_client import MqttBridge
from wisun import WiSunLink
from cmd_router import CmdRouter
from node_store import NodeStore

GID = "gw001"
CERT_DIR = "/etc/mosquitto/certs"   # 프로비저닝해서 받은 ca.crt, gw001.crt, gw001.key 있는 폴더
BROKER_HOST = "xxx.xxx.xxx.xxx"     # 서버에서 준 broker 주소
BROKER_PORT = 8883                  # TLS 포트

CMD_TOPIC = f"wisun/gw/cmd/{GID}"   # 서버 → 게이트웨이 명령
UPLINK_TOPIC = "wisun/uplink"       # 게이트웨이 → 서버 데이터

def on_mqtt_message(client, userdata, msg):
    print(f"[MQTT] recv topic={msg.topic}, payload={msg.payload!r}")

    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print("[MQTT] JSON decode error:", e)
        return

def main():
    # 1) NodeStore / Wi-SUN / Router 준비
    store = NodeStore(path="nodes_store.cbor", cap=20)
    wisun = WiSunLink("/dev/ttyAMA2", 9600, store=store)
    router = CmdRouter(wisun=wisun, mqtt=None, reg=None)  # 실제 생성자에 맞게 조정

    # 2) MQTT 클라이언트 생성
    mqtt = MqttBridge(host=BROKER_HOST, port=BROKER_PORT, base="wisun")
    mqtt.configure_tls_min12_from(CERT_DIR)

    # 3) 브로커 접속
    mqtt.connect(client_id=GID)   # username/token 쓰는 구조면 여기에 맞게 수정

    # 4) 콜백 등록 + 명령 토픽 subscribe
    mqtt._client.on_message = on_mqtt_message   # MqttBridge 안에 wrapper가 있으면 그걸 써도 됨
    mqtt._client.subscribe(CMD_TOPIC, qos=1)

    print(f"[MQTT] connected, sub: {CMD_TOPIC}")

    # 5) 루프를 계속 돌려서 서버에서 오는 걸 기다림
    mqtt._client.loop_forever()

if __name__ == "__main__":
    main()
