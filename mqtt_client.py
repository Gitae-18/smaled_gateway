import json, cbor2, paho.mqtt.client as mqtt
import json, os, tempfile, threading, time
import ssl
import traceback
import logging
PROVISIONING_REQUEST_TOPIC = "devices/provisioning/request"
PROVISIONING_RESPONSE_PREFIX = "devices/provisioning/response"

def _atomic_write_text(text: str, path: str) -> None:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=d, delete=False) as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, path)


class MqttBridge:
    def __init__(self, host, port=8883, base=None, client_id=None, username=None, password=None, scheduler=None, router=None):
        self.host = host
        self.port = port
        self.base = (base or "").rstrip("/")
        self.router = router
        self.client_id = client_id or f"gw-{int(time.time())}"
        self.gwid = self.client_id
        
        self._client = mqtt.Client(client_id=self.client_id, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.scheduler = scheduler
        self.log = logging.getLogger("gw")
        self.log.info('MqttBridge init host=%s port=%d client_id=%s', self.host, self.port, self.client_id)
        if username:
            self._client.username_pw_set(username, password or None)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        
        # 외부에서 붙일 페이로드 핸들러 목록 (payload, topic) -> None
        self._handlers = []

        self._client.on_subscribe = self._on_subscribe
        self._sub_mid_map = {}

        # provisioning state
        self._connected_event = threading.Event()
        self._prov_event = threading.Event()
        self._prov_payload = None  # dict
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

    # --------------- public API ---------------
    def add_handler(self, fn):
        """fn(payload: dict|bytes|..., topic: str) -> None"""
        self._handlers.append(fn)

    def connect(self, host, port):
        host = host or self.host
        port = port or self.port
        print(f"[MQTT] connect() to {host}:{port}, client_id={self.client_id}")
        self.log.info("mqtt connect begin host=%s port=%d", host, port)                
        try:
            rc = self._client.connect(host, port, keepalive=60)
            print(f"[MQTT] connect() returned rc={rc}")
            self.log.info("mqtt connect ok")
        except Exception as e:
            print(f"[MQTT] connect() exception: {e!r}")
            self.log.exception("mqtt connect failed")


    def loop_start(self):
        self._client.loop_start()

    def loop_forever(self):
        self._client.loop_forever()

    def wait_connected(self, timeout=10):
        return self._connected_event.wait(timeout=timeout)

    def set_router(self, router):
        self.router = router

    def _on_disconnect(self, client, userdata, rc, properties=None):
        print(f"[MQTT] disconnected rc={rc}, try reconnect loop...")
        self._connected_event.clear()
        if rc == 0:
            self.log.info("mqtt disconnected (clean) rc=0")
            return  

        self.log.warning("mqtt disconnected (unexpected) rc=%s - reconnect loop start", rc)

        delay = 1
        while True:
            try:
                client.reconnect()
                print("[MQTT] reconnect() success")
                self.log.info("mqtt reconnect success")
                return
            except Exception as e:
                print(f"[MQTT] reconnect() failed: {e!r}, retry in {delay}s")
                self.log.warning("mqtt reconnect failed err=%r retry_in=%ss", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)

    def publish_json(self, topic, obj, qos=1, retain=False):
        """obj를 JSON 문자열로 변환해서 MQTT로 publish"""
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        # paho-mqtt는 str을 넘겨도 내부에서 UTF-8로 인코딩해 줌
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def publish_cbor(self, topic: str, obj):
        self._client.publish(topic, cbor2.dumps(obj))

    # ---------- provisioning (request -> response) ----------
    def provision(self, gateway_id: str, token: str,
                    cert_dir: str = "/etc/mosquitto/certs", timeout: int = 30) -> dict:
                
        self._prov_event.clear()
        self._prov_payload = None

        req = {"gateway_id": gateway_id, "token": token}
        self._client.publish(PROVISIONING_REQUEST_TOPIC, json.dumps(req), qos=1, retain=False)

        ok = self._prov_event.wait(timeout=timeout)
        if not ok or not self._prov_payload:
            raise TimeoutError("[PROV] provisioning response timeout")

        ca_path  = os.path.join(cert_dir, "ca.crt")
        crt_path = os.path.join(cert_dir, f"{gateway_id}.crt")
        key_path = os.path.join(cert_dir, f"{gateway_id}.key")

        _atomic_write_text(self._prov_payload["ca.crt"],    ca_path)
        _atomic_write_text(self._prov_payload["cert.crt"],  crt_path)
        _atomic_write_text(self._prov_payload["private.key"], key_path)

        print(f"[PROV] saved: {ca_path}, {crt_path}, {key_path}")

        return {"paths": {"ca": ca_path, "cert": crt_path, "key": key_path},
                "received": self._prov_payload}

    def configure_tls_from(self, cert_dir: str = "/etc/mosquitto/certs", gateway_id: str = None):
        gid = gateway_id or self.client_id
        ca_path  = os.path.join(cert_dir, "ca.crt")
        crt_path = os.path.join(cert_dir, f"{gid}.crt")
        key_path = os.path.join(cert_dir, f"{gid}.key")
        if not (os.path.exists(ca_path) and os.path.exists(crt_path) and os.path.exists(key_path)):
            raise FileNotFoundError("[PROV] TLS files not found")
        self._client.tls_set(ca_certs=ca_path, certfile=crt_path, keyfile=key_path)
        print(f"[TLS] configured with {crt_path}")

    def reconnect_secure(self, host=None, port=None, keepalive=60):
        """ TLS 설정 후 보안 포트(예: 8883)로 재접속 """
        self._client.disconnect()
        time.sleep(0.3)
        self._client.connect(host or self.host, port or self.port, keepalive=keepalive)
        self._client.loop_start()

    # --------------- internal callbacks ---------------
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            print(f"[MQTT] connect failed: rc={rc}")
            self.log.error("mqtt on_connect failed rc=%s flags=%s", rc, flags)
            self._connected_event.clear()
            return

        self._connected_event.set()

        # 기존 게이트웨이 명령 구독
        gw_cmd = f"gw/{self.client_id}/#"
        node_cmd = f"node/{self.client_id}/ctrl/#"
        #node_cmd_legacy = f"node/{self.client_id}/#" 
        prov_rsp = f"{PROVISIONING_RESPONSE_PREFIX}/{self.client_id}"
        try:
            r, mid = client.subscribe(gw_cmd, qos=1)
            self._sub_mid_map[mid] = gw_cmd
            r, mid = client.subscribe(node_cmd, qos=1)
            self._sub_mid_map[mid] = node_cmd

            r, mid = client.subscribe(f"{PROVISIONING_RESPONSE_PREFIX}/{self.client_id}", qos=1)
            self._sub_mid_map[mid] = f"{PROVISIONING_RESPONSE_PREFIX}/{self.client_id}"
            if self.router:
                self.router.publish_node_inventory(reason="mqtt_connect")
            print(f"[MQTT] connected rc=0, sub: {gw_cmd},{node_cmd}, {PROVISIONING_RESPONSE_PREFIX}/{self.client_id}")
            self.log.info("mqtt connected rc=0 subs=[%s,%s,%s]", gw_cmd, node_cmd, prov_rsp)
        except Exception:
            self.log.exception("mqtt on_connect exception")
  
    def _on_subscribe(self, client, userdata, mid, reasonCodes, properties=None):
        topic = self._sub_mid_map.get(mid, "?")
        print(f"[MQTT] SUBACK mid={mid} topic={topic} reasonCodes={list(reasonCodes)}")

    def handle_gw_cmd(self, topic: str, payload: dict):
        cmd = payload.get("cmd") or topic.split("/")[-1]

        if cmd in ("node_inventory", "inventory"):
            # TODO: 여기서 inventory ack 발행
            self.publish_node_inventory_ack(
                reason=payload.get("reason"),
                msg_id=payload.get("msg_id")
            )
            return

        print(f"[CMD] unknown gw cmd: {cmd}")
        self.publish_cmd_result(cmd, result="fail", reason="unknown_gw_cmd", msg_id=payload.get("msg_id"))

    def _on_message(self, client, userdata, msg):
        t = msg.topic
        gid = self.gwid
        # 1) provisioning response 먼저 처리
        if t.startswith(PROVISIONING_RESPONSE_PREFIX):
            try:
                payload = json.loads(msg.payload.decode())
                if all(k in payload for k in ("ca.crt", "cert.crt", "private.key")):
                    self._prov_payload = payload
                    self._prov_event.set()
                else:
                    print("[PROV] invalid response keys:", payload.keys())
            except Exception as e:
                print("[PROV] response parse error:", e)
            return
        
        if t.startswith(f"gw/{gid}/cmd_result"):
            return
        if t.startswith(f"gw/{gid}/response/"):
            return   
        if t.startswith(f"gw/{gid}/raw"):
            return
        if t.startswith(f"gw/{gid}/boot_success"):
            return
        # 2) 일반 게이트웨이 메시지: CBOR 우선, 실패 시 JSON
        payload = None
        try:
            payload = cbor2.loads(msg.payload)
        except Exception:
            try:
                payload = json.loads(msg.payload.decode())
            except Exception:
                # raw payload로 넘길 수도 있음
                payload = msg.payload
        if t in (f"gw/{gid}/inventory", f"gw/{gid}/mid_lists"):
            if isinstance(payload, dict) and payload.get("cmd") == "node_inventory":
                if self.router and hasattr(self.router, "on_node_inventory"):
                    self.router.on_node_inventory(payload)
            print(f"[MQTT RX INVENTORY] topic={t} payload={payload}")
            return   
        for h in self._handlers:
            try:
                h(payload, t)
            except Exception as e:
                print(f"[HANDLER] exception in {getattr(h, '__name__', str(h))}: {e}")
                traceback.print_exc()
    
    def configure_tls_min12_from(self, cert_dir: str, gateway_id: str):
        ca   = os.path.join(cert_dir, "ca.crt")
        cert = os.path.join(cert_dir, f"{gateway_id}.crt")
        key  = os.path.join(cert_dir, f"{gateway_id}.key")

        print("[TLS] using")
        print(f"  CA   : {ca}")
        print(f"  CERT : {cert}")
        print(f"  KEY  : {key}")

        self._client.tls_set(
            ca_certs=ca,                 # 시스템 CA 사용 또는 완전 무시
            certfile=cert,
            keyfile=key,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
            cert_reqs=ssl.CERT_NONE,  
        )
        self._client.tls_insecure_set(True) 
        self._client._ssl_context.check_hostname = False
    
