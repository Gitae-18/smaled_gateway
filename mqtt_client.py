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

        self._client = mqtt.Client(
            client_id=self.client_id,
            clean_session=True,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.on_publish = self._on_publish
        self._status_topic = f"gw/{self.client_id}/status"
        self._client.will_set(
            self._status_topic,
            payload=json.dumps(
                self._gateway_status_payload("lwt", reason="lwt"),
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            qos=1,
            retain=True,
        )
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
        self._mqtt_keepalive_sec = int(os.getenv("MQTT_KEEPALIVE_SEC", "60") or 60)
        self._link_down_reconnect_delay_sec = float(
            os.getenv("MQTT_LINK_DOWN_RECONNECT_DELAY", str(self._mqtt_keepalive_sec * 2))
            or (self._mqtt_keepalive_sec * 2)
        )
        self._link_down_reconnect_not_before = 0.0
        self._net_iface = os.getenv("MQTT_NET_IFACE", "eth0")
        self._net_watch_interval_sec = float(os.getenv("MQTT_NET_WATCH_INTERVAL", "1.0") or 1.0)
        self._net_watch_started = False
        self._net_watch_stop = threading.Event()
        self._net_last_link_up = None
        self._reconnect_lock = threading.Lock()

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
            rc = self._client.connect(host, port, keepalive=self._mqtt_keepalive_sec)
            print(f"[MQTT] connect() returned rc={rc}")
            self.log.info("mqtt connect ok")
        except Exception as e:
            print(f"[MQTT] connect() exception: {e!r}")
            self.log.exception("mqtt connect failed")


    def loop_start(self):
        self._client.loop_start()
        self._start_network_watchdog()

    def loop_forever(self):
        self._start_network_watchdog()
        self._client.loop_forever()

    def wait_connected(self, timeout=10):
        return self._connected_event.wait(timeout=timeout)

    def set_router(self, router):
        self.router = router

    def _gateway_status_payload(self, status, reason=None):
        payload = {
            "cmd": "gateway_status",
            "gid": self.gwid,
            "status": status,
            "online": status == "online",
            "ts": int(time.time()),
        }
        if reason:
            payload["reason"] = reason
        return payload

    def publish_gateway_status(self, status="online", reason=None):
        return self.publish_json(
            self._status_topic,
            self._gateway_status_payload(status, reason=reason),
            qos=1,
            retain=True,
        )

    def _read_net_link_up(self):
        base = f"/sys/class/net/{self._net_iface}"
        carrier_path = os.path.join(base, "carrier")
        operstate_path = os.path.join(base, "operstate")

        try:
            with open(carrier_path, "r", encoding="ascii") as f:
                carrier = f.read().strip()
            if carrier in ("0", "1"):
                return carrier == "1"
        except Exception:
            pass

        try:
            with open(operstate_path, "r", encoding="ascii") as f:
                operstate = f.read().strip().lower()
            if operstate:
                return operstate not in ("down", "lowerlayerdown", "notpresent")
        except Exception:
            return None

        return None

    def _start_network_watchdog(self):
        if self._net_watch_started:
            return
        self._net_watch_started = True
        t = threading.Thread(target=self._network_watchdog_loop, name="mqtt-net-watchdog", daemon=True)
        t.start()

    def _network_watchdog_loop(self):
        print(f"[NET] watchdog start iface={self._net_iface}")
        self.log.info("network watchdog start iface=%s", self._net_iface)

        while not self._net_watch_stop.is_set():
            link_up = self._read_net_link_up()
            if link_up is not None and link_up != self._net_last_link_up:
                self._net_last_link_up = link_up
                if link_up:
                    print(f"[NET] {self._net_iface} link up; ensure MQTT reconnect")
                    self.log.warning("network link up iface=%s - ensure mqtt reconnect", self._net_iface)
                    if not self._connected_event.is_set():
                        self._start_reconnect_worker("link_up")
                else:
                    print(f"[NET] {self._net_iface} link down; closing MQTT socket")
                    self.log.warning("network link down iface=%s - closing mqtt socket", self._net_iface)
                    self._connected_event.clear()
                    self._link_down_reconnect_not_before = time.monotonic() + self._link_down_reconnect_delay_sec
                    self._close_mqtt_socket_for_link_down()

            self._net_watch_stop.wait(max(0.2, self._net_watch_interval_sec))

    def _close_mqtt_socket_for_link_down(self):
        try:
            self._client._sock_close()
            self.log.warning(
                "mqtt socket force-closed on link down; reconnect delayed %.1fs for lwt",
                self._link_down_reconnect_delay_sec,
            )
        except Exception as e:
            self.log.warning("mqtt socket close on link down failed err=%r", e)

    def _start_reconnect_worker(self, reason):
        t = threading.Thread(target=self._reconnect_until_success, args=(reason,), name="mqtt-reconnect", daemon=True)
        t.start()

    def _reconnect_until_success(self, reason):
        if not self._reconnect_lock.acquire(blocking=False):
            self.log.info("mqtt reconnect already running reason=%s", reason)
            return

        try:
            delay = 1
            while True:
                link_up = self._read_net_link_up()
                if link_up is False:
                    print(f"[MQTT] reconnect wait: {self._net_iface} link down")
                    self.log.warning("mqtt reconnect wait link down iface=%s reason=%s", self._net_iface, reason)
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue

                wait_lwt_sec = self._link_down_reconnect_not_before - time.monotonic()
                if wait_lwt_sec > 0:
                    wait_sec = min(wait_lwt_sec, delay)
                    print(f"[MQTT] reconnect wait: LWT grace {wait_lwt_sec:.1f}s reason={reason}")
                    self.log.warning(
                        "mqtt reconnect wait lwt_grace remaining=%.1fs reason=%s",
                        wait_lwt_sec,
                        reason,
                    )
                    time.sleep(wait_sec)
                    delay = min(delay * 2, 60)
                    continue

                try:
                    self._client.reconnect()
                    print(f"[MQTT] reconnect() success reason={reason}")
                    self.log.info("mqtt reconnect success reason=%s", reason)
                    return
                except Exception as e:
                    print(f"[MQTT] reconnect() failed: {e!r}, retry in {delay}s")
                    self.log.warning("mqtt reconnect failed err=%r retry_in=%ss reason=%s", e, delay, reason)
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
        finally:
            self._reconnect_lock.release()

    def _on_publish(self, client, userdata, mid, *args):
        print(f"[MQTT PUBACK] mid={mid}", flush=True)
        self.log.info("mqtt publish ack mid=%s", mid)

    def _on_disconnect(self, client, userdata, *args):
        # paho callback API v1: (client, userdata, rc)
        # paho callback API v2: (client, userdata, disconnect_flags, reason_code, properties)
        rc = args[1] if len(args) >= 2 else (args[0] if args else None)
        print(f"[MQTT] disconnected rc={rc}, try reconnect loop...")
        self._connected_event.clear()
        try:
            rc_int = int(rc)
        except Exception:
            rc_int = 0 if str(rc).lower() in ("normal disconnection", "success") else -1

        if rc_int == 0:
            self.log.info("mqtt disconnected (clean) rc=0")
            return  

        self.log.warning("mqtt disconnected (unexpected) rc=%s - reconnect loop start", rc)
        self._start_reconnect_worker("mqtt_disconnect")

    def publish_json(self, topic, obj, qos=1, retain=False):
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        print(f"[MQTT PUB] topic={topic} rc={info.rc} mid={info.mid}", flush=True)
        self.log.info( "mqtt publish topic=%s rc=%s mid=%s payload=%s", topic, info.rc, info.mid, payload, )
        return info

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
            self.publish_gateway_status("online", reason="mqtt_connect")
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
    
