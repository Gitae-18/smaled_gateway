from mqtt_client import MqttBridge
from wisun import WiSunLink
from cmd_router import CmdRouter
from store import NodeStore
from registry import NodeRegistry
from node_scheduler import NodeScheduler
import threading, time
from store import NodeStore
import os
import subprocess
from gps_reader import start_gps_thread
from logger.logger import setup_logger

DEFAULT_GID = "gw001"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")


def load_gateway_id(path=None, default=DEFAULT_GID):
    cfg_path = path or os.path.join(CONFIG_DIR, "gw_id.conf")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            gid = f.read().strip()
    except FileNotFoundError:
        return default
    except Exception:
        return default

    return gid or default

BOOT_CHECK_SERVICES = (
    "pigpiod.service",
    "battery-gpio-priming.service",
    "battery_backup.service",
    "multisensor_publisher.service",
)


def _systemctl_state(service_name):
    try:
        active = subprocess.run(
            ["systemctl", "is-active", service_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        failed = subprocess.run(
            ["systemctl", "is-failed", service_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return active.stdout.strip(), failed.stdout.strip()
    except Exception as e:
        return "unknown", f"check_error:{e}"


def _service_boot_ok(service_name):
    active, failed = _systemctl_state(service_name)
    if failed == "failed":
        return False, active, failed
    if service_name == "battery-gpio-priming.service":
        return failed in ("active", "inactive"), active, failed
    return active == "active", active, failed


def publish_boot_success(mqtt, gid, log):
    failed_services = []
    service_states = {}
    for service_name in BOOT_CHECK_SERVICES:
        ok, active, failed = _service_boot_ok(service_name)
        service_states[service_name] = {
            "ok": bool(ok),
            "active": active,
            "failed": failed,
        }
        log.info(
            "boot service check service=%s ok=%s active=%s failed=%s",
            service_name,
            ok,
            active,
            failed,
        )
        if not ok:
            failed_services.append(service_name)

    boot_stats = 0 if failed_services else 1
    topic = f"gw/{gid}/boot_success"
    payload = {"boot_stats": boot_stats}
    if failed_services:
        payload["failed_services"] = failed_services
        payload["service_states"] = service_states
    mqtt.publish_json(topic, payload)
    log.info("boot_success publish topic=%s payload=%s failed_services=%s", topic, payload, failed_services)
    print(f"[BOOT] publish {topic} {payload} failed_services={failed_services}", flush=True)

def main():
    cfg_dir = CONFIG_DIR
    os.makedirs(cfg_dir, exist_ok=True)
    gid = load_gateway_id(os.path.join(cfg_dir, "gw_id.conf"))

    log = setup_logger(
        name="gw",
        log_dir="logs",
        console_level=20,        # INFO
        file_level=20,           # INFO
        error_file_level=30,     # WARNING
        max_bytes=2*1024*1024,
        backup_count=5,
        enable_raw=False,
    )

    log.info("gateway boot gid=%s cfg_dir=%s", gid, cfg_dir)
    def pump_wisun(wisun, router, log):
        drained = 0
        while True:
            try:
                pkt = wisun.get_packet_nowait()
            except Exception as e:
                log.exception("wisun.get_packet_nowait failed: %r", e)
                break

            if not pkt:
                break

            if len(pkt) >= 4:
                mid, payload, ts, mac = pkt
            else:
                mid, payload, ts = pkt
                mac = None
            p0 = payload[0] if isinstance(payload, (bytes, bytearray)) and len(payload) else 0
            is_light_state_event = (
                isinstance(payload, (bytes, bytearray))
                and (
                    (len(payload) >= 1 and payload[0] == 0x15)
                    or (len(payload) >= 8 and payload[7] == 0x15)
                )
            )
            if is_light_state_event:
                dispatch_ts = int(time.time())
                try:
                    queue_delay_sec = max(0, dispatch_ts - int(ts))
                except Exception:
                    queue_delay_sec = None
                print(
                    f"[GW LIGHT_STATE_EVENT QUEUE] mid={mid} recv_ts={ts} "
                    f"dispatch_ts={dispatch_ts} queue_delay_sec={queue_delay_sec}",
                    flush=True,
                )
            print(f"[GW] UL DISPATCH mid={mid} len={len(payload)} p0=0x{p0:02X} ts={ts}", flush=True)

            try:
                router.on_wisun_uplink(mid, payload, ts, mac=mac)
            except Exception as e:
                log.exception("router.on_wisun_uplink failed mid=%s: %r", mid, e)

            drained += 1
        return drained
    
    try:
        store_path = os.path.join(cfg_dir, "nodes_store.bin")
        log.info("init NodeStore path=%s cap=%d", store_path, 20)
        store = NodeStore(path=store_path, cap=20)

        log.info("init WiSunLink dev=%s baud=%d", "/dev/ttyAMA2", 9600)
        wisun = WiSunLink("/dev/ttyAMA2", 9600, store=store)

        log.info("start gps thread poll_interval=%ds", 60)
        start_gps_thread(poll_interval=60)

        log.info("query Wi-SUN CFG via AT+CFG?")
        wisun.send_at_command("AT+CFG?")
        time.sleep(1.0)

        log.info("init NodeScheduler config=%s", "config/node_schedule.config")
        scheduler = NodeScheduler(
            wisun_client=wisun,
            config_path="config/node_schedule.config"
        )

        log.info("init mqtt client host=%s port=%d client_id=%s", "mqtt.hanax.ai", 8883, gid)
        mqtt = MqttBridge(host="mqtt.hanax.ai", port=8883, client_id=gid, username=gid, scheduler=scheduler)
        mqtt.configure_tls_min12_from("/etc/mosquitto/certs", gid)

        reg = NodeRegistry()
        router = CmdRouter(wisun, mqtt, reg, attach_raw_bytes=True, gwid=gid, store=store, scheduler=scheduler)

        mqtt.set_router(router)

        mqtt.add_handler(router.on_server_cmd)

        log.info("mqtt connect begin host=%s port=%d", "mqtt.hanax.ai", 8883)
        mqtt.connect("mqtt.hanax.ai", 8883)
        log.info("mqtt connected, loop_start()")

        mqtt.loop_start()

        if not mqtt.wait_connected(timeout=10):
            log.warning("mqtt connect ack timeout before boot_success publish")
        publish_boot_success(mqtt, gid, log)
        
        log.info("main loop start")

        while True:
            pump_wisun(wisun, router, log)          
            time.sleep(0.01)

    except KeyboardInterrupt:        
        log.warning("keyboard interrupt - shutting down")

    except Exception:        
        log.exception("fatal error in main loop")
        raise


if __name__ == "__main__":
    main()
