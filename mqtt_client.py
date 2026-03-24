import json
import os
import ssl
import tempfile
import threading
import time
import traceback
import logging
from typing import Callable, Any, Optional

import cbor2
import paho.mqtt.client as mqtt


PROVISIONING_REQUEST_TOPIC = "devices/provisioning/request"
PROVISIONING_RESPONSE_PREFIX = "devices/provisioning/response"


def _atomic_write_text(text: str, path: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name

    os.replace(tmp_name, path)


class MqttClient:
    """
    공통 MQTT 클라이언트 골격

    기능:
    - connect / loop_start / loop_forever
    - subscribe 토픽 자동 등록
    - JSON / CBOR publish
    - 수신 메시지 핸들러 등록
    - username/password 인증
    - provisioning 요청/응답
    - TLS 인증서 설정
    - 비정상 disconnect 시 재접속
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        client_id: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        keepalive: int = 60,
        subscribe_topics: Optional[list[str]] = None,
        enable_provisioning_response_subscribe: bool = False,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id or f"client-{int(time.time())}"
        self.keepalive = keepalive

        self.log = logger or logging.getLogger(__name__)
        self._handlers: list[Callable[[Any, str], None]] = []

        self._client = mqtt.Client(
            client_id=self.client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )

        if username:
            self._client.username_pw_set(username, password or None)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe

        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

        self._sub_mid_map: dict[int, str] = {}
        self._subscribe_topics = subscribe_topics or []

        self._enable_prov_rsp_sub = enable_provisioning_response_subscribe
        self._prov_event = threading.Event()
        self._prov_payload: Optional[dict] = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def add_handler(self, fn: Callable[[Any, str], None]) -> None:
        """
        fn(payload, topic) 형태의 핸들러 등록
        """
        self._handlers.append(fn)

    def set_subscribe_topics(self, topics: list[str]) -> None:
        self._subscribe_topics = topics

    def connect(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        target_host = host or self.host
        target_port = port or self.port

        self.log.info("MQTT connect start host=%s port=%s client_id=%s", target_host, target_port, self.client_id)
        self._client.connect(target_host, target_port, keepalive=self.keepalive)

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_forever(self) -> None:
        self._client.loop_forever()

    def disconnect(self) -> None:
        self._client.disconnect()

    def close(self) -> None:
        try:
            self._client.loop_stop()
        finally:
            self._client.disconnect()

    def publish_json(self, topic: str, obj: dict, qos: int = 1, retain: bool = False) -> None:
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def publish_cbor(self, topic: str, obj: Any, qos: int = 1, retain: bool = False) -> None:
        self._client.publish(topic, cbor2.dumps(obj), qos=qos, retain=retain)

    # ------------------------------------------------------------------
    # provisioning
    # ------------------------------------------------------------------

    def provision(
        self,
        gateway_id: str,
        token: str,
        cert_dir: str = "./certs",
        timeout: int = 30,
    ) -> dict:
        """
        provisioning request 발행 후 응답에서 인증서 저장
        """
        self._prov_event.clear()
        self._prov_payload = None

        req = {
            "gateway_id": gateway_id,
            "token": token,
        }

        self.publish_json(PROVISIONING_REQUEST_TOPIC, req, qos=1, retain=False)

        ok = self._prov_event.wait(timeout=timeout)
        if not ok or not self._prov_payload:
            raise TimeoutError("Provisioning response timeout")

        ca_path = os.path.join(cert_dir, "ca.crt")
        crt_path = os.path.join(cert_dir, f"{gateway_id}.crt")
        key_path = os.path.join(cert_dir, f"{gateway_id}.key")

        _atomic_write_text(self._prov_payload["ca.crt"], ca_path)
        _atomic_write_text(self._prov_payload["cert.crt"], crt_path)
        _atomic_write_text(self._prov_payload["private.key"], key_path)

        self.log.info("Provisioning certs saved: %s, %s, %s", ca_path, crt_path, key_path)

        return {
            "paths": {
                "ca": ca_path,
                "cert": crt_path,
                "key": key_path,
            },
            "received": self._prov_payload,
        }

    # ------------------------------------------------------------------
    # TLS
    # ------------------------------------------------------------------

    def configure_tls_from(self, cert_dir: str, gateway_id: Optional[str] = None) -> None:
        gid = gateway_id or self.client_id

        ca_path = os.path.join(cert_dir, "ca.crt")
        crt_path = os.path.join(cert_dir, f"{gid}.crt")
        key_path = os.path.join(cert_dir, f"{gid}.key")

        if not (os.path.exists(ca_path) and os.path.exists(crt_path) and os.path.exists(key_path)):
            raise FileNotFoundError("TLS certificate files not found")

        self._client.tls_set(
            ca_certs=ca_path,
            certfile=crt_path,
            keyfile=key_path,
        )

        self.log.info("TLS configured from cert_dir=%s gateway_id=%s", cert_dir, gid)

    def configure_tls_min12_from(
        self,
        cert_dir: str,
        gateway_id: Optional[str] = None,
        insecure_skip_verify: bool = False,
    ) -> None:
        gid = gateway_id or self.client_id

        ca_path = os.path.join(cert_dir, "ca.crt")
        crt_path = os.path.join(cert_dir, f"{gid}.crt")
        key_path = os.path.join(cert_dir, f"{gid}.key")

        if not (os.path.exists(ca_path) and os.path.exists(crt_path) and os.path.exists(key_path)):
            raise FileNotFoundError("TLS certificate files not found")

        self._client.tls_set(
            ca_certs=ca_path,
            certfile=crt_path,
            keyfile=key_path,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )

        if insecure_skip_verify:
            self._client.tls_insecure_set(True)

        self.log.info("TLS(min12) configured from cert_dir=%s gateway_id=%s", cert_dir, gid)

    def reconnect_secure(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        keepalive: Optional[int] = None,
    ) -> None:
        """
        TLS 설정 후 보안 포트로 재접속
        예: 8883
        """
        target_host = host or self.host
        target_port = port or self.port
        target_keepalive = keepalive or self.keepalive

        self._client.disconnect()
        time.sleep(0.3)
        self._client.connect(target_host, target_port, keepalive=target_keepalive)
        self._client.loop_start()

        self.log.info("Secure reconnect done host=%s port=%s", target_host, target_port)

    # ------------------------------------------------------------------
    # internal callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            self.log.error("MQTT connect failed reason_code=%s flags=%s", reason_code, flags)
            return

        self.log.info("MQTT connected client_id=%s", self.client_id)

        topics = list(self._subscribe_topics)

        if self._enable_prov_rsp_sub:
            topics.append(f"{PROVISIONING_RESPONSE_PREFIX}/{self.client_id}")

        for topic in topics:
            result, mid = client.subscribe(topic, qos=1)
            self._sub_mid_map[mid] = topic
            self.log.info("Subscribe requested topic=%s result=%s mid=%s", topic, result, mid)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None) -> None:
        topic = self._sub_mid_map.get(mid, "?")
        self.log.info("SUBACK mid=%s topic=%s reason_codes=%s", mid, topic, list(reason_codes))

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        self.log.warning("MQTT disconnected reason_code=%s", reason_code)

        if reason_code == 0:
            self.log.info("MQTT disconnected cleanly")
            return

        delay = 1
        while True:
            try:
                client.reconnect()
                self.log.info("MQTT reconnect success")
                return
            except Exception as e:
                self.log.warning("MQTT reconnect failed err=%r retry_in=%ss", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)

    def _on_message(self, client, userdata, msg) -> None:
        topic = msg.topic

        # 1) provisioning response 우선 처리
        if topic == f"{PROVISIONING_RESPONSE_PREFIX}/{self.client_id}":
            try:
                payload = json.loads(msg.payload.decode())
                required = ("ca.crt", "cert.crt", "private.key")

                if all(k in payload for k in required):
                    self._prov_payload = payload
                    self._prov_event.set()
                    self.log.info("Provisioning response received")
                else:
                    self.log.error("Provisioning response missing keys keys=%s", list(payload.keys()))
            except Exception as e:
                self.log.exception("Provisioning response parse error: %r", e)
            return

        # 2) 일반 메시지: CBOR 우선, 실패 시 JSON, 마지막은 raw bytes
        payload: Any
        try:
            payload = cbor2.loads(msg.payload)
        except Exception:
            try:
                payload = json.loads(msg.payload.decode())
            except Exception:
                payload = msg.payload

        # 3) 외부 핸들러 전달
        for handler in self._handlers:
            try:
                handler(payload, topic)
            except Exception as e:
                self.log.error("Handler exception handler=%s err=%r", getattr(handler, "__name__", str(handler)), e)
                traceback.print_exc()