# broker_provisioning_server.py
# 이 스크립트는 MQTT 브로커 위에서 실행되며,
# 게이트웨이로부터의 인증서 프로비저닝 요청을 처리합니다.

import paho.mqtt.client as mqtt
import json
import os
import base64
import sys
import subprocess

# --- 설정 ---
BROKER_HOST = "mqtt.hananet.co.kr"
BROKER_PORT = 1883
PROVISIONING_USER = "provisioning-client"
PROVISIONING_PASS = "provisioning123"

# MQTT 프로비저닝 토픽
PROVISIONING_REQUEST_TOPIC = "devices/provisioning/request"
PROVISIONING_RESPONSE_TOPIC_PREFIX = "devices/provisioning/response/"

# ==================== 수정된 부분 시작 ====================

# 인증서가 저장된 실제 경로로 수정해야 합니다.
# 예시: CERT_PATH = "/home/your_user/certs/"
# ds_server.crt, ds_server.key, ca.crt 파일이 있는 경로를 정확히 입력하세요.
CERT_PATH = "/etc/mosquitto/certs/" 

# ds_server에 전달할 실제 파일명을 지정합니다.
CLIENT_CERT_FILE = os.path.join(CERT_PATH, "ds_server.crt")
CLIENT_KEY_FILE = os.path.join(CERT_PATH, "ds_server.key")
CA_CERT_FILE = os.path.join(CERT_PATH, "ca.crt")
CA_KEY_FILE = os.path.join(CERT_PATH, "ca.key")

# ==================== 수정된 부분 끝 ====================

TOKEN_FILE = '/etc/mosquitto/provisioning_tokens.json'

def load_valid_tokens():
    """JSON 파일에서 유효한 토큰 목록을 읽어옵니다."""
    if not os.path.exists(TOKEN_FILE):
        print(f"🚨 CRITICAL ERROR: 토큰 파일 '{TOKEN_FILE}'을 찾을 수 없습니다.")
        return {}
    with open(TOKEN_FILE, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            print(f"🚨 CRITICAL ERROR: 토큰 파일 '{TOKEN_FILE}'의 형식이 잘못되었습니다.")
            return {}

def generate_and_sign_certificate(client_id):
    certs = {}
    client_key_file = os.path.join(CERT_PATH, f"{client_id}.key")
    client_csr_file = os.path.join(CERT_PATH, f"{client_id}.csr")
    client_cert_file = os.path.join(CERT_PATH, f"app-server.crt" if client_id == "ds_server" else f"{client_id}.crt")
    try:
        key_gen_cmd = ["openssl", "genpkey", "-algorithm", "RSA", "-out", client_key_file]
        subprocess.run(key_gen_cmd, check=True, capture_output=True)
        print(f"   [1/3] {client_id}의 개인 키 생성 완료.")
        csr_gen_cmd = ["openssl", "req", "-new", "-key", client_key_file, "-out", client_csr_file, "-subj", f"/CN={client_id}"]
        subprocess.run(csr_gen_cmd, check=True, capture_output=True)
        print(f"   [2/3] {client_id}의 CSR 생성 완료.")
        cert_sign_cmd = ["openssl", "x509", "-req", "-in", client_csr_file, "-CA", CA_CERT_FILE, "-CAkey", CA_KEY_FILE, "-CAcreateserial", "-out", client_cert_file, "-days", "3650"]
        subprocess.run(cert_sign_cmd, check=True, capture_output=True)
        print(f"   [3/3] CA로 {client_id}의 인증서 서명 완료.")
        with open(CA_CERT_FILE, 'r') as f: certs["ca.crt"] = f.read()
        with open(client_cert_file, 'r') as f: certs["cert.crt"] = f.read()
        with open(client_key_file, 'r') as f: certs["private.key"] = f.read()
        os.remove(client_csr_file)
        return certs
    except FileNotFoundError:
        print("🚨 CRITICAL ERROR: 'openssl' 명령을 찾을 수 없습니다. OpenSSL을 설치해주세요.")
        return None
    except subprocess.CalledProcessError as e:
        print(f"🚨 CRITICAL ERROR: 인증서 생성 중 오류 발생.")
        print(f"   - Stderr: {e.stderr.decode()}")
        return None
    except Exception as e:
        print(f"🚨 CRITICAL ERROR: 파일을 읽는 중 예기치 않은 오류 발생: {e}")
        return None

def on_connect(client, userdata, flags, rc, properties):
    if rc == 0:
        print("✅ 브로커 프로비저닝 서버가 브로커에 연결되었습니다.")
        client.subscribe(PROVISIONING_REQUEST_TOPIC)
        print(f"✅ 토픽 구독: {PROVISIONING_REQUEST_TOPIC}")
    else:
        print(f"❌ 브로커 연결 실패: {rc}")

# --- ⬇️ 여기가 수정된 부분입니다 ⬇️ ---
def on_message(client, userdata, msg):
    print(f"\n📥 [프로비저닝 요청 수신] Topic: {msg.topic}")
    
    if msg.topic == PROVISIONING_REQUEST_TOPIC:
        try:
            payload = json.loads(msg.payload.decode())
            client_id = payload.get("gateway_id")
            token = payload.get("token")
            
            if not (client_id and token):
                print("⚠️ 요청 페이로드에 gateway_id 또는 token이 없습니다.")
                return

            print(f" - Client ID: {client_id}, Token: {token}")
            
            # 파일에서 최신 토큰 목록을 읽어옴
            valid_tokens = load_valid_tokens()
            
            # 읽어온 토큰 목록으로 유효성 검사
            if valid_tokens.get(client_id) != token:
                print("❌ [인증 실패] 유효하지 않은 토큰입니다. 인증서 발급을 거부합니다.")
                return
            
            print("   [인증 성공] 토큰이 유효합니다. 인증서 생성을 시작합니다.")
            certs = generate_and_sign_certificate(client_id)
            
            if certs:
                response_topic = f"{PROVISIONING_RESPONSE_TOPIC_PREFIX}{client_id}"
                client.publish(response_topic, json.dumps(certs))
                print(f"✅ [응답 발행] {response_topic}로 인증서 전송 완료.")
            else:
                print("❌ 인증서 생성 오류로 인해 응답에 실패했습니다.")
        except Exception as e:
            print(f"⚠️ 메시지 처리 중 오류 발생: {e}")
# --- ⬆️ 여기가 수정된 부분입니다 ⬆️ ---

if __name__ == '__main__':
    if not all(os.path.exists(f) and os.path.getsize(f) > 0 for f in [CA_CERT_FILE, CA_KEY_FILE]):
        print("🚨 CRITICAL ERROR: 'certs' 디렉터리에 'ca.crt'와 'ca.key' 파일이 없거나 비어있습니다.")
        sys.exit(1)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="broker-provisioning-server")
    client.on_connect = on_connect
    client.on_message = on_message
    client.username_pw_set(PROVISIONING_USER, PROVISIONING_PASS)
    client.connect(BROKER_HOST, BROKER_PORT)

    print("✅ 브로커 프로비저닝 서버가 시작되었습니다.")
    client.loop_forever()
