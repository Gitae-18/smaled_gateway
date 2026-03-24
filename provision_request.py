#!/usr/bin/env python3
# provision_request.py
#
# 게이트웨이에서 실행해서
# 1) devices/provisioning/request 로 {gateway_id, token} 전송
# 2) 응답으로 온 인증서를 /etc/mosquitto/certs 에 저장

import time
from mqtt_client import MqttBridge


# ======= 여기 두 개만 네 환경에 맞게 수정하면 됨 =======

BROKER_HOST = "mqtt.hananet.co.kr"   # 브로커 주소
BROKER_PORT = 1883                   # 프로비저닝용 포트(평문)

GATEWAY_ID = "gw002"                # /etc/mosquitto/provisioning_tokens.json 의 키
TOKEN = "13e2a40a-0efb-4e19-8da9-584a3f1c7b48"  # 같은 파일에 있는 UUID 토큰 값 그대로

MQTT_USER = "provisioning-client"
MQTT_PASS = "provisioning123"

#CERT_DIR = "/etc/mosquitto/certs"    # 인증서 저장할 경로
CERT_DIR = "/home/pi/certs_gw002"
# ===============================================


def main():
    print(f"[PROV] gateway_id={GATEWAY_ID} 로 프로비저닝 요청 시작")

    # client_id 를 gateway_id 와 맞춰두는 게 깔끔함
    m = MqttBridge(
        host=BROKER_HOST,
        port=BROKER_PORT,
        base="wisun",
        client_id=GATEWAY_ID,
        username=MQTT_USER,      # ★ 추가
        password=MQTT_PASS,      # ★ 추가
    )

    # 브로커 접속 + 루프 시작
    m.connect(BROKER_HOST,BROKER_PORT)
    m.loop_start()           
    time.sleep(1.0)

    try:
        # 토큰 들고 프로비저닝 요청
        result = m.provision(
            gateway_id=GATEWAY_ID,
            token=TOKEN,
            cert_dir=CERT_DIR,
            timeout=30,       # 30초 안에 응답 없으면 TimeoutError
        )

        paths = result["paths"]
        print("[PROV] 프로비저닝 완료!")
        print(f"  CA   : {paths['ca']}")
        print(f"  CERT : {paths['cert']}")
        print(f"  KEY  : {paths['key']}")

    except Exception as e:
        print(f"[PROV] 프로비저닝 실패: {e}")

    finally:
        # 깔끔하게 정리
        m.disconnect()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
