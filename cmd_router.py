from store import NodeStore
import time
import threading, os
import struct
import subprocess, datetime
import json, queue
import logging
import string
from collections import defaultdict, deque

CMD_SNAP = 0x13 
CMD_SET_MID_CH = 0x21
CMD_SET_SETTING = 0x31
CMD_GET_FFT   = 0x27
CMD_GET_UID   = 0x22
CMD_GET_CH    = 0x24
CMD_SET_CH    = 0x25
CMD_GET_STATUS = 0x30

CMD_GET_VI    = 0x26   
CMD_GET_INFO  = 0x40
#CMD_GET_INFO = 0x27 
#CMD_GET_NODE_INFO = 0x40
CMD_SET_POWER = 0x28  
CMD_SET_NODE_INFO      = 0x29
CMD_SET_TIME_INTERVAL  = 0x2A

T_SNAP = 0x01
SNAP_BIN_FMT = "<B12sfffBffffIBb"
SNAP_BIN_SIZE = struct.calcsize(SNAP_BIN_FMT)

ACK_T = 0x10
ACK_NODE_CFG_T = 0x20
ACK_BIN_FMT = "<B12sIBb"
ACK_BIN_SIZE = struct.calcsize(ACK_BIN_FMT)

STATUS_T = 0x02
STATUS_BIN_FMT  = "<B12sfffBIBb"
STATUS_BIN_SIZE = struct.calcsize(STATUS_BIN_FMT)

GET_CH_T = 0x24
GET_CH_BIN_FMT = "<B12sHBbB"   # 설명: B( t ) 12s( uid ) H( msg_id ) B(ok) b(err) B(ch)
GET_CH_BIN_SIZE = struct.calcsize(GET_CH_BIN_FMT)

NODE_INFO_T = 0x40
NODE_INFO_HDR_FMT = "<B12sHBbH"   # t, uid, msg_id(u16), ok, err(i8), text_len(u16)
NODE_INFO_HDR_SIZE = struct.calcsize(NODE_INFO_HDR_FMT)
NODE_INFO_TEXT_MAX = 240

T_NODEINFO_BIN = 0x14
NODEINFO_FMT = "<B12sHBBHBBBBBB8sHH"   # Little-endian, packed
NODEINFO_SIZE = struct.calcsize(NODEINFO_FMT)

ENV_KEYS = ("temp","humi","pm1","pm25","pm10","co2","voc","ch2o","co","o3","no2","ax","ay","az")
def read_env_latest(path="/home/pi/config/env_latest.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
def _strip_at_nul(b: bytes) -> bytes:
        """payload 안에 0x00이 섞여오면 그 지점에서 텍스트 종료로 간주"""
        i = b.find(b"\x00")
        return b if i < 0 else b[:i]

def _clean_text_bytes(b: bytes) -> bytes:
    # NUL 제거 + \r 유지
    b = _strip_at_nul(b)
    i = b.find(b"\x03")
    if i != -1:
        b = b[:i]
    return b

def _extract_env_values(latest: dict) -> dict:
    env_values = {}
    if isinstance(latest, dict):
        for k in ENV_KEYS:
            if k in latest:
                env_values[k] = latest[k]
        if "ts" in latest:
            env_values["ts"] = latest["ts"]
    return env_values

def _looks_like_text_payload(b: bytes) -> bool:
    if not b:
        return False
    b2 = _clean_text_bytes(b)
    if len(b2) < 8:
        return False
    allowed = set(bytes(string.printable, "ascii")) | {0x0A, 0x0D, 0x09}
    ok = sum(1 for x in b2 if x in allowed)
    ratio = ok / len(b2)
    has_kv = (b"=" in b2) and (b"\n" in b2 or b"\r" in b2)
    return ratio >= 0.85 and has_kv

def _parse_kv_text(b: bytes) -> dict:
    b2 = _clean_text_bytes(b)
    text = b2.decode("ascii", errors="replace")
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
    kv = {}
    for ln in lines:
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv[k.strip()] = v.strip()
    return {"text": text, "lines": lines, "kv": kv}     

def parse_nodeinfo_bin(b: bytes):
    if len(b) < NODEINFO_SIZE:
        return None
    (t, uid, msg_id, ok, gid, mid,
     dev, dsp, rch0, rch1, txp, mode,
     mac8, fw_major, fw_minor) = struct.unpack_from(NODEINFO_FMT, b, 0)

    return {
        "t": t,
        "uid": uid.hex(),
        "msg_id": msg_id,
        "ok": int(ok),
        "gid": int(gid),
        "mid": int(mid),
        "dev": int(dev),
        "dsp": int(dsp),
        "rch": [int(rch0), int(rch1)],
        "txp": int(txp),
        "mode": int(mode),
        "mac": mac8.hex(),
        "fw": {"major": int(fw_major), "minor": int(fw_minor)},
    }
    
def cut_cfg_text(payload: bytes) -> bytes:
    # 1) 가장 확실한 종료: ":<ETX>"
    m = payload.find(b":\x03")
    if m != -1:
        return payload[:m]  # ':' 이전까지만

    # 2) 혹시 ETX만 오는 경우도 대비
    m = payload.find(b"\x03")
    if m != -1:
        return payload[:m]

    # 3) 마지막 안전장치: 비 printable 나오면 거기서 컷
    out = bytearray()
    for x in payload:
        if x in (0x0A, 0x0D, 0x09):   # \n \r \t 허용
            out.append(x)
            continue
        if 0x20 <= x <= 0x7E:        # printable ASCII
            out.append(x)
            continue
        break
    return bytes(out)  
NODE_CMD_MAP = {
  "get_node_uid":        {"code": CMD_GET_UID,  "need": []},
  "get_channel":         {"code": CMD_GET_CH,   "need": []},
  "set_channel":         {"code": CMD_SET_CH,   "need": ["ch"]},          # 서버 payload 필드명에 맞추기
  "get_fft_data":        {"code": CMD_GET_FFT,  "need": []},
  
  "get_voltage_current": {"code": CMD_GET_VI,   "need": []},
  "get_node_info":       {"code": CMD_GET_INFO, "need": []},
  "set_power_ctrl":      {"code": CMD_SET_POWER, "need": ["status"]},       # 예: 0/1
  "set_node_info":     {"code": CMD_SET_NODE_INFO,     "need": ["info"]},
  "set_time_interval": {"code": CMD_SET_TIME_INTERVAL, "need": ["period_min"]},
}

class CmdRouter:
    def __init__(self, wisun, mqtt, reg, attach_raw_bytes: bool = False,  gwid: str = "gw001", store: NodeStore | None = None, scheduler = None,):
        self.wisun = wisun
        self.mqtt = mqtt
        self.reg  = reg
        self.attach_raw_bytes = attach_raw_bytes  
        self.gwid = gwid
        self.store = store
        self.scheduler = scheduler

        self.snap_batch_period_sec = 60  # 1분 주기
        self._snap_cycle_seen_mids = set()
        self._snap_cycle_lock = threading.Lock()
        self._snap_cycle_start_ts = time.time()

        self.pending_status = {}   
        self.next_mid = 1
        self._snap_frag_store = {}
        self.pending_multi = {}
        self.cache = {
            "snap": {},    
            "status": {},  
            "uid": {},   
        }
        self.pending = defaultdict(deque)
        self.log = logging.getLogger("gw")

        self._stop_event = threading.Event()

        t = threading.Thread(target=self._snap_batch_loop, daemon=True)
        t.start()
        
    
        self._env_thread = threading.Thread(
            target=self._publish_gw_periodic_env,
            args=(300.0,),
            daemon=True
        )
        self._env_thread.start()

    def _snap_online_window_sec(self) -> int:
        period_sec = None
        if self.scheduler is not None and hasattr(self.scheduler, "get_snap_period_sec"):
            try:
                period_sec = self.scheduler.get_snap_period_sec()
            except Exception:
                period_sec = None

        if period_sec is None or period_sec <= 0:
            return 120

        return max(120, int(period_sec) + int(self.snap_batch_period_sec))
        
    
    def _publish_gw_periodic_env(self, period_sec: float = 60.0):
        topic = f"gw/{self.gwid}/gw_env"

        while not self._stop_event.is_set():
            latest = read_env_latest()
            env_values = _extract_env_values(latest)

            if env_values:
                msg = {
                    "cmd": "gw_env",          # ← 이벤트/스냅샷용 cmd
                    "gid": self.gwid,
                    "reason": "periodic",
                    "ts": int(time.time()),
                    "values": env_values,
                }
                self.mqtt.publish_json(topic, msg)
                print("[PUB] gw/env:", msg)

            time.sleep(period_sec)

    def stop(self):
        if hasattr(self, "_stop_event"):
            self._stop_event.set()

    def on_server_cmd(self, payload: dict, topic: str):
        print(f"[MQTT RX CMD] topic={topic} payload={payload}")        
        root = gw_id = topic_cmd = None
        try:
            if not isinstance(payload, dict):
                print(f"[CMD_ROUTER] ignore non-dict payload topic={topic}")
                return
            cmd = payload.get("cmd")
            target = payload.get("target", "node")
            data = payload.get("payload") or payload.get("data") or {}
            parts = topic.split("/")
            root = parts[0] if len(parts) > 0 else ""
            gw_id = parts[1] if len(parts) > 1 else None
            raw_msg_id = payload.get("msg_id")
            try:
                msg_id_int = int(raw_msg_id) if raw_msg_id is not None else (int(time.time() * 1000) & 0xFFFFFFFF)
            except (TypeError, ValueError):
                msg_id_int = int(time.time() * 1000) & 0xFFFFFFFF

            if raw_msg_id is None:
                raw_msg_id = msg_id_int
            topic_cmd = parts[2] if len(parts) > 2 else None

            if root == "node" and gw_id == self.gwid and topic_cmd == "response":
                return

            if root == "gw" and gw_id == self.gwid and topic_cmd in ("gw_env", "mid_lists"):
                return

            # topic이 gw/{gwid}/{cmd} 형태면 target을 gw로 보정
            if root == "gw" and gw_id == self.gwid and topic_cmd:
                if topic_cmd not in ("cmd_result", "response", "raw", "gw_env", "mid_lists"):
                    if not target or target == "node":
                        target = "gw"
                    if not cmd:
                        cmd = topic_cmd
        except Exception as e:
            print(f"[CMD_ROUTER] exception: {e} topic={topic} root={root} gw_id={gw_id} topic_cmd={topic_cmd} payload={payload}")
            return
        # -----------------------------
        # 공통 헬퍼
        # -----------------------------
        def _publish_gw(resp: dict):
            self.mqtt.publish_json(f"gw/{self.gwid}/cmd_result", resp)

        node_topic = lambda c: f"node/{self.gwid}/response/{c}"

        def _publish_node(cmd_key: str, resp: dict):
            self.mqtt.publish_json(node_topic(cmd_key), resp)

        def _base_cmd(c: str) -> str:
            return c[:-4] if c.endswith("_ack") else c

        def _node_fail(cmd_key: str, reason: str, mid: int | None = None):
            base = _base_cmd(cmd_key)
            resp = {
                "cmd": f"{base}_ack",
                "gid": self.gwid,
                "msg_id": raw_msg_id,
                "result": "fail",
                "reason": reason,
            }
            if mid is not None:
                resp["mid"] = mid
            _publish_node(base, resp)   
            print(f"[CMD] {base} fail:", resp)

        # =========================================================================
        # Gateway commands (gw/* 또는 target=gw)
        # =========================================================================
        if target in ("gw", "gateway"):
            GW_ALIAS = {"change_gw_id": "set_gw_id"}
            gw_cmd = GW_ALIAS.get(cmd, cmd)

            def _gw_set_gw_id():
                new_gwid = (
                    data.get("id")
                    or data.get("new_gwid")
                    or data.get("gwid")
                    or data.get("gid")
                )

                if not new_gwid:
                    resp = {
                        "cmd": "set_gw_id_ack",
                        "old_gwid": self.gwid,
                        "new_gwid": None,
                        "result": "fail",
                        "reason": "missing_id",
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    print("[CMD] set_gw_id_ack:", resp)
                    return

                save_ok = False
                try:
                    os.makedirs("/home/pi/config", exist_ok=True)
                    with open("/home/pi/config/gw_id.conf", "w", encoding="utf-8") as f:
                        f.write(str(new_gwid).strip() + "\n")
                    save_ok = True
                except Exception as e:
                    print("[CMD] set_gw_id: save failed:", e)

                resp = {
                    "cmd": "set_gw_id_ack",
                    "old_gwid": self.gwid,
                    "new_gwid": new_gwid,
                    "result": "success" if save_ok else "fail",
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                print("[CMD] set_gw_id_ack:", resp)

                if save_ok:
                    print("[CMD] set_gw_id: rebooting to apply new gwid:", new_gwid)

                    def _reboot_later():
                        time.sleep(2)
                        os.system("sudo reboot")

                    threading.Thread(target=_reboot_later, daemon=True).start()

            def _gw_reboot():
                delay_sec = int(data.get("delay_sec", 1) or 1)
                resp = {
                    "cmd": "reboot_ack",
                    "gid": self.gwid,
                    "result": "success",
                    "delay_sec": delay_sec,
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                print("[CMD] reboot_ack:", resp)

                def _reboot_later():
                    print(f"[CMD] reboot: rebooting in {delay_sec} sec...")
                    time.sleep(delay_sec)
                    os.system("sudo reboot")

                threading.Thread(target=_reboot_later, daemon=True).start()

            def _gw_get_gw_info():
                info = {
                    "gwid": self.gwid,
                    "uid": getattr(self, "hw_uid", None),
                    "fw": {"version": getattr(self, "fw_version", "unknown")},
                }
                resp = {"cmd": "get_gw_info_ack", "info": info, "msg_id": raw_msg_id}
                _publish_gw(resp)
                print("[CMD] get_gw_info_ack:", resp)

            def _gw_get_wisun_status():
                # 1) 신규: AT+CFG? 텍스트 반환 helper
                if hasattr(self.wisun, "at_get_cfg"):
                    try:
                        cfg_text = self.wisun.at_get_cfg(timeout=2.0)
                        resp = {
                            "cmd": "get_wisun_status_ack",
                            "wisun": {"cfg": cfg_text},
                            "msg_id": raw_msg_id,
                        }
                        _publish_gw(resp)
                        print("[CMD] get_wisun_status_ack:", resp)
                        return
                    except Exception as e:
                        resp = {
                            "cmd": "get_wisun_status_ack",
                            "wisun": {},
                            "result": "fail",
                            "reason": f"at_error:{e}",
                            "msg_id": raw_msg_id,
                        }
                        _publish_gw(resp)
                        print("[CMD] get_wisun_status_ack fail:", resp)
                        return

                # 2) 구버전 호환
                if hasattr(self.wisun, "get_status_dict"):
                    wisun_stat = self.wisun.get_status_dict()
                    resp = {
                        "cmd": "get_wisun_status_ack",
                        "wisun": wisun_stat,
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    print("[CMD] get_wisun_status_ack:", resp)
                    return

                resp = {
                    "cmd": "get_wisun_status_ack",
                    "wisun": {},
                    "result": "fail",
                    "reason": "no_helper",
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                print("[CMD] get_wisun_status_ack fail:", resp)

            def _gw_get_env_info(raw_msg_id=None):
                latest = read_env_latest()
                env_values = _extract_env_values(latest)

                resp = {
                    "cmd": "get_env_info_ack",
                    "gid": self.gwid,          # gw 응답이면 gid 넣는 게 보통 더 좋음
                    "values": env_values,
                    "ok": bool(env_values),
                    "msg_id": raw_msg_id,
                    "ts": int(time.time()),
                }
                _publish_gw(resp)
                print("[CMD] get_env_info_ack:", resp)

            def _gw_set_gw_time():
                time_str = data.get("time") or data.get("datetime") or data.get("value")
                if not time_str:
                    resp = {
                        "cmd": "set_gw_time_ack",
                        "result": "fail",
                        "reason": "missing_time",
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    print("[CMD] set_gw_time_ack fail:", resp)
                    return

                try:
                    datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                    subprocess.run(["sudo", "date", "-s", time_str], check=True)
                    subprocess.run(["sudo", "hwclock", "-w"], check=False)

                    resp = {
                        "cmd": "set_gw_time_ack",
                        "result": "success",
                        "time": time_str,
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    print("[CMD] set_gw_time_ack:", resp)
                except Exception as e:
                    resp = {
                        "cmd": "set_gw_time_ack",
                        "result": "fail",
                        "reason": str(e),
                        "msg_id": raw_msg_id,
                    }
                    _publish_gw(resp)
                    print("[CMD] set_gw_time_ack fail:", resp)

            def _gw_send_ping():
                resp = {
                    "cmd": "send_ping_ack",
                    "gid": self.gwid,
                    "ts": int(time.time()),
                    "result": "success",
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                print("[CMD] send_ping_ack:", resp)

            gw_handlers = {
                "set_gw_id": _gw_set_gw_id,
                "reboot": _gw_reboot,
                "get_gw_info": _gw_get_gw_info,
                "get_wisun_status": _gw_get_wisun_status,
                "get_env_info": _gw_get_env_info,
                "set_gw_time": _gw_set_gw_time,
                "send_ping": _gw_send_ping,
            }

            h = gw_handlers.get(gw_cmd)
            if not h:
                resp = {
                    "cmd": gw_cmd,
                    "result": "fail",
                    "reason": "unknown_gw_cmd",
                    "msg_id": raw_msg_id,
                }
                _publish_gw(resp)
                print("[CMD] unknown gw cmd:", gw_cmd)
                return

            h()
            return

        # =========================================================================
        # Node commands (node/* 또는 기본 target=node)
        # =========================================================================
        NODE_ALIAS = {"get_status": "get_node_status"}
        node_cmd = NODE_ALIAS.get(cmd, cmd)

        """ NODE_FORWARD_TABLE: dict[str, dict] = {
            "get_fft_data":         {"cmd_code": None, "need": ["mid"]},
            "get_voltage_current":  {"cmd_code": None, "need": ["mid"]},
            "get_node_uid":         {"cmd_code": None, "need": ["mid"]},
            "get_channel":          {"cmd_code": None, "need": ["mid"]},
            "get_node_info":        {"cmd_code": None, "need": ["mid"]},
            "set_power_ctrl":       {"cmd_code": None, "need": ["mid"]},
            "set_channel":          {"cmd_code": None, "need": ["mid", "ch"]},
            "set_node_info":        {"cmd_code": None, "need": ["mid"]},
            "set_time_interval":    {"cmd_code": None, "need": ["mid", "period_min"]},
        } """

        def _node_forward_simple(cmd_key: str):
            spec = NODE_CMD_MAP.get(cmd_key)
            if not spec:
                _node_fail(cmd_key, "unknown_cmd")
                return
            data = payload.get("data") or {}
            pdata = payload.get("payload") or {}
            # mid는 서버 포맷에 따라 payload 최상위 / data 안쪽 둘 다 대비
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail(cmd_key, "missing_mid")
                return

            # need 필드 검사
            for k in spec.get("need", []):
                if (k not in data) and (k not in pdata):
                    _node_fail(cmd_key, f"missing_{k}", mid=mid)
                    return

            cmd_code = int(spec["code"])

            # extra 패킹(필요한 것만 최소로)
            extra = b""
            if cmd_key == "set_channel":
                ch = int(data.get("ch", payload.get("ch", 0)) or 0) & 0xFF
                extra = bytes([ch])

            elif cmd_key == "set_power_ctrl":
                # data/pdata는 위에서 이미 만들어졌지만, 안전하게 재사용해도 됨
                st_present = ("status" in data) or ("status" in pdata)
                if not st_present:
                    _node_fail(cmd_key, "missing_status", mid=mid)
                    return

                st = int(data.get("status", pdata.get("status", 0)) or 0)

                # 노드 코드 기준: cmd 바이트가 0x10/0x11
                if st:       # ON
                    cmd_code = 0x10          # LIGHT_ON
                    flags2   = 0x01          # flags bit0=1 -> 실제 ON
                else:        # OFF
                    cmd_code = 0x11          # LIGHT_OFF
                    flags2   = 0x00

                tx_msg_id16 = int(msg_id_int) & 0xFFFF

                try:
                    self._pending_push(mid=mid, api_cmd="set_power_ctrl", srv_msg_id=raw_msg_id,
                                    want="ack", tx_msg_id=tx_msg_id16, ttl_sec=3.0)
                    data = payload.get("data") or {}
                    status = int(data.get("status", 0))  # 기본 0
                    status = 1 if status else 0          # 0/1로 정규화

                    extra = bytes([status])          
                    self.wisun.send_cmd_bytes(mid, cmd_code, msg_id=tx_msg_id16, flags=flags2, extra=extra)

                    self.log.info("DL cmd=set_power_ctrl mid=%d code=0x%02X flags=0x%02X status=%d msg_id=%s tx_msg_id=%d",
                        mid, cmd_code, flags2, status, raw_msg_id, tx_msg_id16)

                    _publish_node("set_power_ctrl", {
                        "cmd": "set_power_ctrl_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "msg_id": raw_msg_id,
                        "result": "success",
                        "queued": True,
                        "data": {"status": status},
                    })
                    print(f"[CMD] set_power_ctrl → mid={mid} code=0x{cmd_code:02X} flags=0x{flags2:02X}")
                except Exception as e:
                    _node_fail("set_power_ctrl", f"send_error:{e}", mid=mid)

                return  


            elif cmd_key == "set_time_interval":
                period_min = int(data.get("period_min", payload.get("period_min", 0)) or 0)
                period_min = max(1, min(120, period_min))  
                extra = bytes([period_min & 0xFF])

            elif cmd_key == "set_node_info":
                info = data.get("info", payload.get("info", {}))
                s = json.dumps(info, ensure_ascii=False).encode("utf-8")
                if len(s) > 200:  
                    raise ValueError("info too long")
                extra = bytes([len(s) & 0xFF]) + s

            else:
                extra = b""

            tx_msg_id16 = int(msg_id_int) & 0xFFFF
            try:

                self._pending_push(
                    mid=mid,
                    api_cmd=cmd_key,
                    srv_msg_id=raw_msg_id,     # 서버에서 온 msg_id(문자열/원본)
                    want="ack",
                    tx_msg_id=tx_msg_id16,      # 노드로 보낸 msg_id(int)
                    ttl_sec=3.0
                )
                
                self.wisun.send_cmd_bytes(mid, cmd_code, msg_id=tx_msg_id16, flags=0x00, extra=extra)
                self.log.info("DL cmd=%s mid=%d code=0x%02X msg_id=%s tx_msg_id=%d extra_len=%d",
                cmd_key, mid, cmd_code, raw_msg_id, tx_msg_id16, len(extra))
                
                _publish_node(cmd_key, {
                    "cmd": f"{cmd_key}_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,
                    "result": "success",
                    "queued": True,
                })
                print(f"[CMD] {cmd_key} → mid={mid} code=0x{cmd_code:02X} extra={extra.hex()}")
            except Exception as e:
                _node_fail(cmd_key, f"send_error:{e}", mid=mid)

        def _node_get_voltage_current_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_voltage_current", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)
            if not r or r.get("voltage") is None or r.get("current") is None:
                self._ack_fail("get_voltage_current", raw_msg_id, mid, "no_data")
                return

            self._ack_ok("get_voltage_current", raw_msg_id, mid, {
                "voltage": r.get("voltage"),
                "current": r.get("current"),
                "temperature": r.get("temperature"),
                "data_ts": r.get("ts"),
            })

        def _node_get_fft_data_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_fft_data", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)
            if not r or not r.get("fft"):
                self._ack_fail("get_fft_data", raw_msg_id, mid, "no_data")
                return

            self._ack_ok("get_fft_data", raw_msg_id, mid, {
                "fft": r.get("fft"),
                "voltage": r.get("voltage"),
                "current": r.get("current"),
                "temperature": r.get("temperature"),
                "data_ts": r.get("ts"),
            })

        def _node_get_node_uid_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            print(f"[HANDLER] get_node_uid_cached enter mid={mid}", flush=True)

            if not mid:
                _node_fail("get_node_uid", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)
            uid = (r.get("uid") if r else None)

            if uid:  
                print(f"[HANDLER] get_node_uid_cached HIT uid={uid}", flush=True)              
                _publish_node("get_node_uid", {
                    "cmd": "get_node_uid_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,
                    "result": "success",
                    "data": {"uid": uid},
                    "data_ts": int(r.get("ts") or 0) if isinstance(r, dict) else 0,
                })
                return

            print("[HANDLER] get_node_uid_cached MISS -> forward", flush=True)
            _node_forward_simple("get_node_uid")

        def _node_get_channel_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_channel", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)
            ch = (r.get("ch") if r else None)   
            if ch is not None:
                _publish_node("get_channel", {
                    "cmd":"get_channel_ack","gid":self.gwid,"mid":mid,"msg_id":raw_msg_id,
                    "result":"success","data":{"ch": int(ch)}, "data_ts": int(r.get("ts") or 0)
                })
                return

            _node_forward_simple("get_channel")

        def _node_get_node_info_cached():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_node_info", "missing_mid")
                return

            r = self._store_latest_by_mid(mid)

            info = {}
            if isinstance(r, dict):
                # 흔히 있는 것들(있으면 넣고, 없으면 스킵)
                for k in (
                    "uid", "mac", "ch", "channel",
                    "online", "last_ts", "ts",
                    "voltage", "current", "temperature", "light_on",
                    "gps", "latitude", "longitude",
                    "snap_period_min",
                    "fft", "ultrasonic",
                ):
                    if k in r and r[k] is not None:
                        info[k] = r[k]

            # 응답에 최소한 uid라도 있으면 "성공"으로 보고 반환
            if info:
                _publish_node("get_node_info", {
                    "cmd": "get_node_info_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,          
                    "result": "success",
                    "data": info,
                    "data_ts": int(info.get("ts") or info.get("last_ts") or 0),
                })
                return

            _node_forward_simple("get_node_info")

        def _node_get_status():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("get_node_status", "missing_mid")
                return

            tx_msg_id16 = int(msg_id_int) & 0xFFFF
            try:
                self._pending_push(
                    mid=mid,
                    api_cmd="get_node_status",
                    srv_msg_id=raw_msg_id,
                    want="ack",
                    tx_msg_id=tx_msg_id16,
                    ttl_sec=3.0
                )
                self.wisun.send_cmd_bytes(mid, CMD_GET_STATUS, msg_id=tx_msg_id16, flags=0x00, extra=b"")
                self.log.info("DL cmd=get_node_status mid=%d code=0x%02X msg_id=%s tx_msg_id=%d",
                            mid, CMD_GET_STATUS, raw_msg_id, tx_msg_id16)
                print(f"[CMD] get_node_status → mid={mid} code=0x{CMD_GET_STATUS:02X} tx_msg_id={tx_msg_id16}")
            except Exception as e:
                _node_fail("get_node_status", f"send_error:{e}", mid=mid)

        """ def _node_set_setting():
            mid = int(payload.get("mid", 0) or 0)
            if not mid:
                _node_fail("set_setting", "missing_mid")
                return

            d = payload.get("payload") or payload.get("data") or {}

            def parse_time(s: str | None):
                if not s:
                    return 0, 0
                try:
                    p = s.split(":")
                    h = int(p[0])
                    m = int(p[1]) if len(p) > 1 else 0
                    return h, m
                except Exception:
                    return 0, 0

            saving_start_h, saving_start_m = parse_time(d.get("saving_start_time"))
            saving_end_h, saving_end_m = parse_time(d.get("saving_end_time"))

            on_off_mode     = int(d.get("on_off_mode", 0) or 0)
            on_corr_mode    = int(d.get("on_correction_mode", 0) or 0)
            on_corr_time    = int(d.get("on_correction_time", 0) or 0)
            off_corr_mode   = int(d.get("off_correction_mode", 0) or 0)
            off_corr_time   = int(d.get("off_correction_time", 0) or 0)
            forced_time     = int(d.get("forced_time", 0) or 0)
            saving_mode     = int(d.get("saving_mode", 0) or 0)
            snap_enable     = int(d.get("snap_enable", 1) or 1)
            snap_period_min = int(d.get("snap_period_min", 1) or 1)

            if snap_period_min <= 0 or snap_period_min > 120:
                snap_period_min = max(1, min(120, snap_period_min))

            base_extra = [
                on_off_mode           & 0xFF,
                on_corr_mode          & 0xFF,
                on_corr_time          & 0xFF,
                off_corr_mode         & 0xFF,
                off_corr_time         & 0xFF,
                forced_time           & 0xFF,
                saving_mode           & 0xFF,
                saving_start_h        & 0xFF,
                saving_start_m        & 0xFF,
                saving_end_h          & 0xFF,
                saving_end_m          & 0xFF,
                (1 if snap_enable else 0) & 0xFF,
                snap_period_min       & 0xFF,
                mid & 0xFF,
                (mid >> 8) & 0xFF,
            ]
            extra = bytes(base_extra)
            self._pending_push(
                    mid=mid,
                    api_cmd="set_setting",
                    srv_msg_id=raw_msg_id,
                    want="ack",
                    tx_msg_id=msg_id_int,
                    ttl_sec=3.0
                )
            self.wisun.send_cmd_bytes(mid, CMD_SET_SETTING, msg_id=msg_id_int, flags=0x00, extra=extra)
            print(f"[CMD] set_setting → mid={mid} extra={extra.hex()}") """
        def _node_apply_setting(api_cmd: str):
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail(api_cmd, "missing_mid")
                return

            d = payload.get("payload") or payload.get("data") or data or {}

            def parse_time(s: str | None):
                if not s:
                    return 0, 0
                try:
                    p = str(s).split(":")
                    h = int(p[0])
                    m = int(p[1]) if len(p) > 1 else 0
                    return h, m
                except Exception:
                    return 0, 0

            saving_start_h, saving_start_m = parse_time(d.get("saving_start_time"))
            saving_end_h, saving_end_m     = parse_time(d.get("saving_end_time"))

            on_off_mode     = int(d.get("on_off_mode", 0) or 0)
            on_corr_mode    = int(d.get("on_correction_mode", 0) or 0)
            on_corr_time    = int(d.get("on_correction_time", 0) or 0)
            off_corr_mode   = int(d.get("off_correction_mode", 0) or 0)
            off_corr_time   = int(d.get("off_correction_time", 0) or 0)
            forced_time     = int(d.get("forced_time", 0) or 0)
            saving_mode     = int(d.get("saving_mode", 0) or 0)
            snap_enable     = int(d.get("snap_enable", 1) or 1)

            # snap_period_min 우선, 없으면 cycle도 허용(서버 payload 호환)
            snap_period_min = int(d.get("snap_period_min", d.get("cycle", 1)) or 1)
            if snap_period_min <= 0 or snap_period_min > 120:
                snap_period_min = max(1, min(120, snap_period_min))

            extra = bytes([
                on_off_mode & 0xFF,
                on_corr_mode & 0xFF,
                on_corr_time & 0xFF,
                off_corr_mode & 0xFF,
                off_corr_time & 0xFF,
                forced_time & 0xFF,
                saving_mode & 0xFF,
                saving_start_h & 0xFF,
                saving_start_m & 0xFF,
                saving_end_h & 0xFF,
                saving_end_m & 0xFF,
                (1 if snap_enable else 0) & 0xFF,
                snap_period_min & 0xFF,
                mid & 0xFF,
                (mid >> 8) & 0xFF,
            ])
            tx_msg_id16 = int(msg_id_int) & 0xFFFF
            try:                
                self._pending_push(
                    mid=mid,
                    api_cmd=api_cmd,
                    srv_msg_id=raw_msg_id,
                    want="ack",
                    tx_msg_id=tx_msg_id16,
                    ttl_sec=3.0
                )

                
                self.wisun.send_cmd_bytes(mid, CMD_SET_SETTING, msg_id=tx_msg_id16, flags=0x00, extra=extra)

                
                _publish_node(api_cmd, {
                    "cmd": f"{api_cmd}_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": tx_msg_id16,
                    "result": "success",
                    "queued": True,
                })

                print(f"[CMD] {api_cmd} → mid={mid} (TX:0x31) extra={extra.hex()}")
            except Exception as e:
                _node_fail(api_cmd, f"send_error:{e}", mid=mid)

        def _node_set_setting():
            print(f"[HANDLER] set_setting → mid={payload.get('mid')} msg_id={payload.get('msg_id')} topic={topic}")
            _node_apply_setting("set_setting")

        def _node_set_node_info():
            print(f"[HANDLER] set_node_info → mid={payload.get('mid')} msg_id={payload.get('msg_id')} topic={topic}")
            _node_apply_setting("set_node_info")

        def _node_set_mid_chan():
            target_uid = (
                data.get("uid")
                or data.get("target_uid")
                or payload.get("uid")
                or payload.get("target_uid")
            )
            new_mid = data.get("mid")
            new_ch  = data.get("ch")

            if not target_uid:
                _node_fail("set_mid_chan", "missing_uid")
                return
            if new_mid is None or new_ch is None:
                _node_fail("set_mid_chan", "missing_mid_or_ch")
                return

            try:
                new_mid = int(new_mid)
                new_ch = int(new_ch)
            except Exception:
                _node_fail("set_mid_chan", "bad_mid_or_ch")
                return

            try:
                uid_bytes = bytes.fromhex(str(target_uid))
                if len(uid_bytes) != 12:
                    raise ValueError("uid must be 12 bytes")
            except Exception as e:
                _node_fail("set_mid_chan", f"bad_uid:{e}")
                return

            extra = bytearray()
            extra.extend(uid_bytes)
            extra.append((new_mid >> 8) & 0xFF)
            extra.append(new_mid & 0xFF)
            extra.append(new_ch & 0xFF)

            try:
                self.wisun.send_cmd_bytes(0, CMD_SET_MID_CH, msg_id=msg_id_int, flags=0x00, extra=bytes(extra))
                _publish_node("set_mid_chan", {
                    "cmd": "set_mid_chan_ack",
                    "gid": self.gwid,
                    "uid": uid_bytes.hex(),
                    "mid": new_mid,
                    "ch": new_ch,
                    "msg_id": raw_msg_id,
                    "result": "success",
                    "queued": True,
                })
                print(f"[CMD] set_mid_chan → UID={uid_bytes.hex()} MID={new_mid} CH={new_ch}, msg_id={msg_id_int}")
            except Exception as e:
                _node_fail("set_mid_chan", f"send_error:{e}")

        def _node_setid_key():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("setid_key", "missing_mid")
                return
            key = (data.get("key") or "").encode("utf-8")
            try:
                self.wisun.send(mid, key)
                _publish_node("setid_key", {
                    "cmd": "setid_key_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,
                    "result": "success",
                })
            except Exception as e:
                _node_fail("setid_key", f"send_error:{e}", mid=mid)

        def _node_ping():
            mid = int(payload.get("mid", 0) or data.get("mid", 0) or 0)
            if not mid:
                _node_fail("ping", "missing_mid")
                return
            try:
                self.wisun.send(mid, b"PING")
                _publish_node("ping", {
                    "cmd": "ping_ack",
                    "gid": self.gwid,
                    "mid": mid,
                    "msg_id": raw_msg_id,
                    "result": "success",
                    "queued": True,
                })
            except Exception as e:
                _node_fail("ping", f"send_error:{e}", mid=mid)

        def _node_get_all_status():
            self._handle_get_all_status(payload)

        node_handlers = {
            "get_node_status": _node_get_status,
            "set_setting": _node_set_setting,
            "set_node_info": _node_set_node_info, 
            "set_mid_chan": _node_set_mid_chan,
            "setid_key": _node_setid_key,
            "ping": _node_ping,
            "get_all_status": _node_get_all_status,
            
            "get_fft_data":         _node_get_fft_data_cached,
            "get_voltage_current":  _node_get_voltage_current_cached,
            "get_node_uid":         _node_get_node_uid_cached,
            "get_channel":          _node_get_channel_cached,
            "get_node_info":        lambda *_a, **_k: _node_forward_simple("get_node_info"),
            "set_power_ctrl":       lambda *_a, **_k: _node_forward_simple("set_power_ctrl"),
            "set_channel":          lambda *_a, **_k: _node_forward_simple("set_channel"),            
            "set_time_interval":    lambda *_a, **_k: _node_forward_simple("set_time_interval"),
        }

        h = node_handlers.get(node_cmd)
        if not h:
            _node_fail(node_cmd or "unknown", "unknown_node_cmd")
            return

        h()
        return

    def _now(self) -> float:
        return time.time()

    def _cache_put(self, kind: str, mid: int, msg: dict):
        self.cache[kind][mid] = (self._now(), msg)

    def _cache_get(self, kind: str, mid: int, max_age_sec: float):
        v = self.cache[kind].get(mid)
        if not v:
            return None
        ts, msg = v
        if (self._now() - ts) > max_age_sec:
            return None
        return msg
    
    def _store_latest_by_mid(self, mid: int):
        if self.store is None:
            return None
        try:
            nodes = self.store.all_nodes()  # 이미 쓰고 있음
        except Exception:
            return None

        candidates = [
            r for r in nodes
            if int(r.get("mid") or 0) == int(mid)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.get("ts", 0))

    def _node_inventory_meta(self, rec: dict, now: int | None = None) -> tuple[int, bool]:
        if now is None:
            now = int(time.time())

        last_ts = int(rec.get("last_ts") or rec.get("ts") or 0)
        mid = int(rec.get("mid") or 0)

        if mid <= 0 or last_ts <= 0:
            return last_ts, False

        if self.reg is not None and hasattr(self.reg, "is_online"):
            try:
                return last_ts, bool(self.reg.is_online(mid, now))
            except Exception:
                pass

        online_window_sec = self._snap_online_window_sec()
        return last_ts, (now - last_ts) <= online_window_sec

    def _remember_node_seen(
        self,
        *,
        mid: int,
        ts: int,
        uid: str | None = None,
        mac: str | None = None,
        voltage=None,
        current=None,
        temperature=None,
        fft=None,
    ):
        if self.store is None:
            return None

        try:
            mid_int = int(mid or 0)
        except (TypeError, ValueError):
            return None

        if mid_int <= 0:
            return None

        prev = self._store_latest_by_mid(mid_int)
        prev_sig = None if prev is None else (
            int(prev.get("mid") or 0),
            prev.get("uid"),
            prev.get("mac"),
        )

        try:
            rec = self.store.upsert(
                uid=uid,
                mid=mid_int,
                mac=mac,
                ultrasonic=None,
                voltage=voltage,
                current=current,
                temperature=temperature,
                fft=fft,
                ts=ts,
            )
        except Exception as e:
            print("[NodeStore] update error(remember_node_seen):", e)
            return prev

        if not rec:
            return prev

        cur_sig = (
            int(rec.get("mid") or 0),
            rec.get("uid"),
            rec.get("mac"),
        )
        if prev_sig != cur_sig and self.mqtt is not None:
            try:
                self.publish_node_inventory(reason="uplink_seen")
            except Exception as e:
                print("[INV] publish error(uplink_seen):", e)

        return rec

    def publish_node_inventory(self, reason: str = "mqtt_connect"):
        nodes = self.store.all_nodes() if self.store is not None else []
        now = int(time.time())

        items = []
        for r in nodes:
            mid = int(r.get("mid") or 0)
            if mid == 0:
                continue
            last_ts, online = self._node_inventory_meta(r, now=now)
            items.append({
                "mid": mid,
                "uid": r.get("uid"),
                "mac": r.get("mac"),
                "online": bool(online),
                "last_ts": last_ts,
            })

        # mid 기준 정렬(보기 좋게)
        items.sort(key=lambda x: x["mid"])

        payload = {
            "cmd": "node_inventory",
            "gid": self.gwid,
            "ts": int(time.time()),
            "reason": reason,
            "nodes": items,
            "count": len(items),
        }

        topic = f"gw/{self.gwid}/mid_lists"  
        self.mqtt.publish_json(topic, payload)
        print(f"[MQTT TX INVENTORY] topic={topic} count={len(items)}")


    def on_node_inventory(self, payload: dict):
        # nodes: [{mid, uid, mac, online, last_ts}, ...]
        nodes = payload.get("nodes", [])
        reason = payload.get("reason")
        ts = payload.get("ts")
        gid = payload.get("gid")

        # 여기서 nodes_store.bin 또는 in-memory store seed/update
        # self.store.seed_inventory(gid, nodes, ts=ts, reason=reason)
        print(f"[INV] seed ok gid={gid} count={len(nodes)} reason={reason} ts={ts}")

    def _publish_node_ack(self, api_cmd: str, resp: dict):
        # 네 규칙대로 topic을 맞춰
        topic = f"node/{self.gwid}/response/{api_cmd}"
        self.mqtt.publish_json(topic, resp)
        print("[MQTT TX ACK]", topic, resp)

    def _ack_ok(self, api_cmd: str, msg_id, mid: int, data: dict):
        self._publish_node_ack(api_cmd, {
            "cmd": f"{api_cmd}_ack",
            "gid": self.gwid,
            "mid": mid,
            "msg_id": msg_id,
            "result": "success",
            "data": data,
        })

    def _ack_fail(self, api_cmd: str, msg_id, mid: int, reason: str):
        self._publish_node_ack(api_cmd, {
            "cmd": f"{api_cmd}_ack",
            "gid": self.gwid,
            "mid": mid,
            "msg_id": msg_id,
            "result": "fail",
            "reason": reason,
        })

    def _pending_push(self, mid: int, api_cmd: str, srv_msg_id, want: str, tx_msg_id: int | None = None, ttl_sec: float = 2.0):        
        self.pending[mid].append({
            "api": api_cmd,
            "srv_msg_id": srv_msg_id,
            "want": want,
            "tx_msg_id": tx_msg_id,
            "t": self._now(),
            "ttl": float(ttl_sec),
        })

    def _pending_pop_ack(self, mid: int, tx_msg_id: int | None):
        q = self.pending.get(mid)
        if not q:
            return None

        now = self._now()

        # timeout 정리
        while q and (now - q[0]["t"]) > q[0]["ttl"]:
            expired = q.popleft()
            if expired.get("want") == "ack": 
                self._ack_fail(expired["api"], expired.get("srv_msg_id"), mid, "timeout")
            
        # tx_msg_id가 없으면 첫 ack 대기만 pop
        if tx_msg_id is None:
            for _ in range(len(q)):
                item = q[0]
                if item.get("want") == "ack":
                    return q.popleft()
                q.rotate(-1)
            return None

        # tx_msg_id로 정확 매칭
        for _ in range(len(q)):
            item = q[0]
            if item.get("want") == "ack" and item.get("tx_msg_id") == tx_msg_id:
                return q.popleft()
            q.rotate(-1)

        return None    

    def _snap_batch_loop(self):
        while True:
            time.sleep(self.snap_batch_period_sec)
            try:
                self._publish_snap_batch()
            except Exception as e:
                print("[SNAP_BATCH] error:", e)

    

    def _handle_get_all_status(self, req: dict):
        """
        서버에서 get_all_status 요청 왔을 때 처리.
        NodeStore에 저장된 값 기준으로 전체 노드 상태 응답.
        """
        if self.store is None:
            return

        nodes = self.store.all_nodes()
        if not nodes:
            # 노드 하나도 없는 경우
            resp = {
                "cmd": "get_all_status",
                "gid": self.gwid,
                "ts": int(time.time()),
                "nodes": [],
            }
            topic = f"node/{self.gwid}/get_all_status"
            self.mqtt.publish_json(topic, resp)
            print("[MQTT TX GET_ALL_STATUS empty]", resp)
            return

        now = int(time.time())

        # mid 기준 정렬
        nodes_sorted = sorted(
            nodes,
            key=lambda r: ((r.get("mid") or 0), (r.get("uid") or ""))
        )

        # reg(심박/마지막 접속시간) 같은 걸로 online 여부 판별 가능하면 같이 붙이기
        nodes_payload = []
        for r in nodes_sorted:
            mid = r.get("mid")
            uid = r.get("uid")
            last_ts = int(r.get("ts") or 0)

            # 예: 마지막 접속 2분 이내면 online 으로 표시
            online = (now - last_ts) <= 120

            nodes_payload.append({
                "mid":   mid,
                "uid":   uid,
                "mac":   r.get("mac"),
                "last_ts": last_ts,
                "online": online,
                "data": {
                    "voltage":     r.get("voltage"),
                    "current":     r.get("current"),
                    "temperature": r.get("temperature"),
                    "fft":         r.get("fft"),
                },
            })

        resp = {
            "cmd": "get_all_status",
            "gid": self.gwid,
            "ts": now,
            "nodes": nodes_payload,
            # 필요하면 요청 쪽 msg_id / rqid 같은 것도 그대로 되돌려주기
            "msg_id": req.get("msg_id"),
        }

        topic = f"node/{self.gwid}/get_all_status"
        self.mqtt.publish_json(topic, resp)
        print("[MQTT TX GET_ALL_STATUS]", resp)

    def _publish_snap_batch(self):
        """
        NodeStore 기준으로 전체 노드 상태를 mid 순으로 정렬해서
        node/{gwid}/snap_batch 토픽으로 한 번에 전송.
        이번 라운드에서 보고 안 한 노드는 online=False로 표시.
        """
        if self.store is None:
            return

        nodes = self.store.all_nodes()
        if not nodes:
            return

        now = int(time.time())

        # 이번 라운드에서 스냅을 보낸 노드 목록 (mid 집합)
        with self._snap_cycle_lock:
            seen_mids = set(self._snap_cycle_seen_mids)
            # 새 라운드를 위해 초기화
            self._snap_cycle_seen_mids.clear()
            self._snap_cycle_start_ts = now

        # mid, uid 기준으로 정렬
        nodes_sorted = sorted(
            nodes,
            key=lambda r: ((r.get("mid") or 0), (r.get("uid") or ""))
        )

        nodes_payload = []
        for r in nodes_sorted:
            mid = r.get("mid")
            uid = r.get("uid")
            # 이번 라운드에서 보고했는지 여부
            last_ts = int(r.get("ts") or 0)
            online_window_sec = self._snap_online_window_sec()
            is_online = (now - last_ts) <= online_window_sec if last_ts > 0 else False
            print(f"[SNAP_BATCH NODE] mid={mid} uid={uid} mac={r.get('mac')} last_ts={last_ts} window={online_window_sec} online={is_online}")

            node_obj = {
                "mid":   mid,
                "uid":   uid,
                "mac":   r.get("mac"),
                "last_ts": last_ts,
                "online": bool(is_online),   # 보고 안 한 애들은 False
                "status": "ok" if is_online else "disconnected",
                "data": {
                    "voltage":     r.get("voltage"),
                    "current":     r.get("current"),
                    "temperature": r.get("temperature"),
                    "fft":         r.get("fft"),
                },
            }
            nodes_payload.append(node_obj)

        payload = {
            "cmd": "snap_batch",
            "gid": self.gwid,
            "ts": now,
            "nodes": nodes_payload,
        }

        topic_batch = f"node/{self.gwid}/snap"
        self.mqtt.publish_json(topic_batch, payload)
        print(f"[MQTT TX SNAP_BATCH] topic={topic_batch} payload={payload}")

    def _alloc_mid(self) -> int:
        m = self.next_mid
        self.next_mid = (self.next_mid + 1) & 0xFFFF
        if self.next_mid == 0:
            self.next_mid = 1
        return m
    
    def on_wisun_uplink(self, mid: int, payload, ts: int, mac: str | None = None):
        try:
            self.reg.touch(mid)
            raw_hex = payload.hex(" ") if isinstance(payload, (bytes, bytearray)) else ""
            print(f"[GW] UL mid={mid} len={len(payload)} p0=0x{payload[0]:02X} mac={mac} raw={raw_hex}")
            topic_snap = f"node/{self.gwid}/snap"
            topic_cmd  = lambda cmd: f"node/{self.gwid}/response/{cmd}"
            topic_raw  = f"gw/{self.gwid}/raw"
            looks_like_transport = False
            msg = None
            body = payload
            target_mid = None
            ttl = None
            cmd_code = None
            flags = None
            node_msg_id = None
            if isinstance(payload, (bytes, bytearray)):
                b = bytes(payload)  
                body = b
                if len(body) >= NODEINFO_SIZE and body[0] == T_NODEINFO_BIN:
                    info = parse_nodeinfo_bin(body)
                    self._remember_node_seen(
                        mid=mid,
                        ts=ts,
                        uid=info["uid"],
                        mac=mac or info.get("mac"),
                    )
                    resp = {
                        "cmd": "get_node_info_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": info["uid"],
                        "msg_id": info["msg_id"],
                        "node_msg_id": node_msg_id,
                        "result": "success" if info["ok"] else "fail",
                        "err_code": 0 if info["ok"] else 1,
                        "ts": ts,
                        "data": info,
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    self.mqtt.publish_json(topic_cmd("get_node_info"), resp)
                    print("[MQTT TX GET_NODE_INFO_BIN]", resp)
                    return       
                # 0) 먼저: transport header 없는 순수 Ack/Snap인지 체크 (가장 안전)
                if len(payload) >= ACK_BIN_SIZE and payload[0] in (ACK_T, ACK_NODE_CFG_T):
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                elif len(payload) >= SNAP_BIN_SIZE and payload[0] == T_SNAP:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                elif len(payload) >= STATUS_BIN_SIZE and payload[0] == STATUS_T:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None 
                elif len(payload) >= GET_CH_BIN_SIZE and payload[0] == GET_CH_T:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                elif len(payload) >= NODE_INFO_HDR_SIZE and payload[0] == NODE_INFO_T:
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                else:
                    # 1) transport header 분리 시도
                    body = payload
                    target_mid = ttl = cmd_code = flags = None
                    node_msg_id = None
                    
                    if len(payload) >= 7:
                        target_mid  = payload[0] | (payload[1] << 8)
                        ttl         = payload[2]
                        cmd_code    = payload[3]
                        flags       = payload[4]
                        node_msg_id = (payload[5] << 8) | payload[6]
                        body        = payload[7:]

                    
                    is_body_ack  = (len(body) >= ACK_BIN_SIZE  and body[0] in (ACK_T, ACK_NODE_CFG_T))
                    is_body_snap = (len(body) >= SNAP_BIN_SIZE and body[0] == T_SNAP)
                    is_body_status = (len(body) >= STATUS_BIN_SIZE and body[0] == STATUS_T)
                    is_body_getch  = (len(body) >= 1 and body[0] == 0x24)
                    is_body_info   = (len(body) >= 1 and body[0] == 0x40)
                    
                    if not (is_body_ack or is_body_snap or is_body_status or is_body_getch or is_body_info):
                        
                        if (ttl is not None and 1 <= ttl <= 20 and
                            cmd_code is not None and cmd_code in (0x31,)):
                            print(f"[GW] DROP echoed/downlink-like uplink mid={mid} "
                                f"target_mid={target_mid} ttl={ttl} cmd=0x{cmd_code:02X} "
                                f"len={len(payload)} raw={raw_hex}")
                            return            
                if len(body) >= GET_CH_BIN_SIZE and body[0] == GET_CH_T:
                    try:
                        t_val, uid_bytes, msg_id16, ok, err_code, ch = struct.unpack(GET_CH_BIN_FMT, body[:GET_CH_BIN_SIZE])
                    except struct.error as e:
                        print("[GW] GetChResp unpack error:", e, "len=", len(payload), "raw=", raw_hex)
                        return

                    uid_str = uid_bytes.hex()
                    self._remember_node_seen(mid=mid, ts=ts, uid=uid_str, mac=mac)
                    match_id = int(node_msg_id) if node_msg_id is not None else int(msg_id16)

                    # pending 있으면 서버 msg_id로 치환, 없으면 match_id 사용
                    pend = self._pending_pop_ack(mid, match_id)
                    srv_msg_id = (pend["srv_msg_id"] if pend else match_id)

                    resp = {
                        "cmd": "get_channel_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "msg_id": srv_msg_id,
                        "node_msg_id": match_id,
                        "result": "success" if ok else "fail",
                        "err_code": int(err_code),
                        "ts": ts,
                        "data": {"ch": int(ch)},
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    self.mqtt.publish_json(topic_cmd("get_channel"), resp)
                    print("[MQTT TX GET_CHANNEL_ACK]", resp)
                    return
                if len(body) >= NODE_INFO_HDR_SIZE and body[0] == NODE_INFO_T:
                    try:
                        t_val, uid_bytes, msg_id16, ok, err_code, text_len = struct.unpack(
                            NODE_INFO_HDR_FMT, body[:NODE_INFO_HDR_SIZE]
                        )
                    except struct.error as e:
                        print("[GW] NodeInfoHdr unpack error:", e, "len=", len(payload), "raw=", raw_hex)
                        return

                    # 길이 방어
                    if text_len > NODE_INFO_TEXT_MAX:
                        text_len = NODE_INFO_TEXT_MAX
                    if len(body) < NODE_INFO_HDR_SIZE + text_len:
                        print("[GW] NodeInfoText short body:", "need", NODE_INFO_HDR_SIZE + text_len, "got", len(body))
                        return

                    text_bytes = body[NODE_INFO_HDR_SIZE:NODE_INFO_HDR_SIZE + text_len]
                    text = text_bytes.decode("ascii", errors="ignore")

                    uid_str = uid_bytes.hex()
                    self._remember_node_seen(mid=mid, ts=ts, uid=uid_str, mac=mac)
                    match_id = int(node_msg_id) if node_msg_id is not None else int(msg_id16)

                    pend = self._pending_pop_ack(mid, match_id)
                    srv_msg_id = (pend["srv_msg_id"] if pend else match_id)

                    resp = {
                        "cmd": "get_node_info_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "msg_id": srv_msg_id,
                        "node_msg_id": match_id,
                        "result": "success" if ok else "fail",
                        "err_code": int(err_code),
                        "ts": ts,
                        "data": {
                            "cfg_raw": text,
                            "lines": [ln for ln in text.splitlines() if ln.strip()],
                        },
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }
                    
                    self.mqtt.publish_json(topic_cmd("get_node_info"), resp)
                    print("[MQTT TX GET_NODE_INFO_ACK]", resp)
                    return
                # 1) AckBin (body[0] == 0x10)
                if len(body) >= ACK_BIN_SIZE and body[0] in (ACK_T, ACK_NODE_CFG_T):
                    try:
                        t_val, uid_bytes, msg_id32, ok, err_code = struct.unpack(ACK_BIN_FMT, body[:ACK_BIN_SIZE])
                    except struct.error as e:
                        print("[GW] AckBin unpack error:", e, "len=", len(payload), "raw=", raw_hex)
                        return
                        """ self.mqtt.publish_json(topic_raw, {
                            "cmd": "ack_unpack_error",
                            "gid": self.gwid,
                            "mid": mid,
                            "ts": ts,
                            "error": str(e),
                            "payload_hex": raw_hex,
                        })
                        return """

                    uid_str = uid_bytes.hex()
                    self._remember_node_seen(mid=mid, ts=ts, uid=uid_str, mac=mac)
                    result = "success" if ok else "fail"
                    
                    match_id = int(node_msg_id) if node_msg_id is not None else int(msg_id32 & 0xFFFF)

                    pend = self._pending_pop_ack(mid, match_id)
                    api = pend["api"] if pend else "unknown"

                    resp = {
                        "cmd": f"{api}_ack",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "msg_id": (pend["srv_msg_id"] if pend else match_id),  # 서버 msg_id 우선
                        "node_msg_id": match_id,
                        "result": result,
                        "err_code": int(err_code),
                        "ts": ts,

                        # 디버깅용(원하면)
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }

                    self.mqtt.publish_json(topic_cmd(api), resp)
                    self.log.info("UL ack api=%s mid=%d node_msg_id=%s result=%s err=%d",
                    api, mid, match_id, result, int(err_code))
                    print(f"[MQTT TX {api.upper()}_ACK]", resp)
                    return
                # status Bin 
                if len(body) >= STATUS_BIN_SIZE and body[0] == STATUS_T:
                    try:
                        t_val, uid_bytes, volt, curr, temp, light_on, msg_id32, ok, err_code = \
                            struct.unpack(STATUS_BIN_FMT, body[:STATUS_BIN_SIZE])
                    except struct.error as e:
                        print("[GW] StatusBin unpack error:", e, "len=", len(payload), "raw=", raw_hex)
                        self.mqtt.publish_json(topic_raw, {
                            "cmd": "status_unpack_error",
                            "gid": self.gwid,
                            "mid": mid,
                            "ts": ts,
                            "error": str(e),
                            "payload_hex": raw_hex,
                        })
                        return

                    uid_str = uid_bytes.hex()
                    self._remember_node_seen(
                        mid=mid,
                        ts=ts,
                        uid=uid_str,
                        mac=mac,
                        voltage=volt,
                        current=curr,
                        temperature=temp,
                    )

                    # match_id 규칙: transport header의 node_msg_id가 있으면 그걸 우선
                    match_id = int(node_msg_id) if node_msg_id is not None else int(msg_id32 & 0xFFFF)

                    # pending에서 api를 뽑아오면 보통 "get_node_status"가 들어있게 됨
                    pend = self._pending_pop_ack(mid, match_id)   # 이름은 ack지만 "요청-응답 매칭" 용도로 재사용 가능
                    api = pend["api"] if pend else "get_node_status"

                    
                    """ if self.store is not None:
                        try:
                            self.store.upsert(
                                uid=uid_str,
                                mid=mid,
                                mac=mac,
                                ultrasonic=None,
                                voltage=volt,
                                current=curr,
                                temperature=temp,
                                ts=ts,
                            )
                        except Exception as e:
                            print("[NodeStore] update error(status_bin):", e) """

                    resp = {
                        "cmd": api,   
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": uid_str,
                        "msg_id": (pend["srv_msg_id"] if pend else match_id),
                        "node_msg_id": match_id,
                        "result": "success" if ok else "fail",
                        "err_code": int(err_code),
                        "ts": ts,
                        "data": {
                            "voltage": float(volt),
                            "current": float(curr),
                            "temperature": float(temp),
                            "light_on": int(light_on),
                            "ok": int(ok),
                        },

                        # 디버깅용(원하면)
                        "rx_target_mid": target_mid,
                        "rx_cmd": cmd_code,
                        "rx_flags": flags,
                        "rx_ttl": ttl,
                    }


                    self.mqtt.publish_json(topic_cmd("get_node_status"), resp)
                    print("[MQTT TX GET_NODE_STATUS]", resp)
                    return

                # 2) SnapBin (body[0] == 0x01)
                if len(body) >= SNAP_BIN_SIZE and body[0] == T_SNAP:
                    try:
                        (
                            t_val,
                            uid_bytes,
                            volt,
                            curr,
                            temp,
                            fft_count,
                            f1, a1,
                            f2, a2,
                            msg_id32,
                            ok,
                            err_code,
                        ) = struct.unpack(SNAP_BIN_FMT, body[:SNAP_BIN_SIZE])
                    except struct.error as e:
                        print("[GW] SnapBin unpack error:", e, "len=", len(payload), "raw=", raw_hex)
                        self.mqtt.publish_json(topic_raw, {
                            "cmd": "uplink_unpack_error",
                            "gid": self.gwid,
                            "mid": mid,
                            "ts": ts,
                            "error": str(e),
                            "payload_hex": raw_hex,
                        })
                        return

                    uid_str = uid_bytes.hex()

                    fft = []
                    if fft_count >= 1:
                        fft.append([f1, a1])
                    if fft_count >= 2:
                        fft.append([f2, a2])

                    self._remember_node_seen(
                        mid=mid,
                        ts=ts,
                        uid=uid_str,
                        mac=mac,
                        voltage=volt,
                        current=curr,
                        temperature=temp,
                        fft=fft,
                    )

                    with self._snap_cycle_lock:
                        self._snap_cycle_seen_mids.add(mid)

                    return

                # 3) 모르는 bytes → raw
                print("[GW] Unknown binary uplink, len=", len(payload), "raw=", raw_hex)
                self.mqtt.publish_json(topic_raw, {
                    "cmd": "uplink_unknown_bin",
                    "gid": self.gwid,
                    "mid": mid,
                    "ts": ts,
                    "payload_hex": raw_hex,
                })
                return

            # 2) dict / list → 기존 JSON/dict 경로
            elif isinstance(payload, dict):
                msg = payload
            elif isinstance(payload, list):
                msg = payload
            else:
                print("[GW] Unknown payload type:", type(payload))
                self.mqtt.publish_json(topic_raw, {
                    "cmd": "uplink_unexpected_type",
                    "gid": self.gwid,
                    "mid": mid,
                    "ts": ts,
                    "py_type": str(type(payload)),
                })
                return

            # --- 여기부터는 "dict로 오는 binary 해석 결과" 처리 로직 ---
            print("[GW-RX-DECODED]", msg)

            # 2-1) get_status 스타일 응답 (rqid 있는 dict)
            if isinstance(msg, dict) and "rqid" in msg:
                rqid = msg.get("rqid")
                ctx = self.pending_status.pop(rqid, None) if rqid is not None else None

                if self.store is not None:
                    try:
                        raw_uid = msg.get("uid")
                        if isinstance(raw_uid, (bytes, bytearray)):
                            uid_str = raw_uid.hex()
                        else:
                            uid_str = raw_uid

                        self._remember_node_seen(
                            mid=mid,
                            ts=ts,
                            uid=uid_str,
                            mac=msg.get("mac") or mac,
                            voltage=msg.get("v"),
                            current=msg.get("i"),
                            temperature=msg.get("tp", msg.get("t")),
                        )
                    except Exception as e:
                        print("[NodeStore] update error(get_status):", e)

                if ctx is not None:
                    resp = {
                        "cmd": "get_node_status",
                        "gid": self.gwid,
                        "mid": mid,
                        "uid": msg.get("uid"),
                        "msg_id": ctx.get("msg_id"),
                        "data": {
                            "ultrasonic": msg.get("ultrasonic"),
                            "voltage":    msg.get("v"),
                            "current":    msg.get("i"),
                            "temperature": msg.get("tp", msg.get("t")),
                        },
                        "result": "success",
                        "timestamp": ctx.get("timestamp_req"),
                    }
                    self.mqtt.publish_json(topic_cmd("get_node_status"), resp)
                    self.log.info("UL status mid=%d node_msg_id=%s result=%s err=%d",
                    mid, match_id, resp["result"], int(err_code))
                    print("[MQTT TX GET_STATUS]", resp)
                    return

            # 2-2) dict 기반 snap (t == "snap")
            if isinstance(msg, dict) and msg.get("t") == "snap":
                raw_uid = msg.get("uid")
                if isinstance(raw_uid, (bytes, bytearray)):
                    uid_str = raw_uid.hex()
                else:
                    uid_str = raw_uid

                self._remember_node_seen(
                    mid=mid,
                    ts=ts,
                    uid=uid_str,
                    mac=msg.get("mac") or mac,
                    voltage=msg.get("volt"),
                    current=msg.get("curr"),
                    temperature=msg.get("temp"),
                    fft=msg.get("fft"),
                )

                with self._snap_cycle_lock:
                    self._snap_cycle_seen_mids.add(mid)

                return

            # 2-3) 나머지 dict/list 는 디버그용으로 raw 토픽에만 올림
            self.mqtt.publish_json(topic_raw, {
                "cmd": "uplink",
                "gid": self.gwid,
                "mid": mid,
                "ts": ts,
                "data": msg,
            })
        except Exception as e:
            import traceback
            print("[GW] on_wisun_uplink EXC:", e, flush=True)
            traceback.print_exc()
    
