from mqtt_client import MqttBridge
from wisun import WiSunLink
from cmd_router import CmdRouter
from store import NodeStore
from registry import NodeRegistry
from node_scheduler import NodeScheduler
import threading, time
from store import NodeStore
import os
from gps_reader import start_gps_thread
from logger.logger import setup_logger

GID = "gw001" 

def main():
    cfg_dir = "config"
    os.makedirs(cfg_dir, exist_ok=True)

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

    log.info("gateway boot gid=%s cfg_dir=%s", GID, cfg_dir)
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

        log.info("init mqtt client host=%s port=%d client_id=%s", "mqtt.hananet.co.kr", 8883, GID)
        mqtt = MqttBridge(host="mqtt.hananet.co.kr", port=8883, client_id=GID, username=GID, scheduler=scheduler)
        mqtt.configure_tls_min12_from("/etc/mosquitto/certs", GID)

        reg = NodeRegistry()
        router = CmdRouter(wisun, mqtt, reg, attach_raw_bytes=True, gwid=GID, store=store, scheduler=scheduler)

        mqtt.set_router(router)

        mqtt.add_handler(router.on_server_cmd)

        log.info("mqtt connect begin host=%s port=%d", "mqtt.hananet.co.kr", 8883)
        mqtt.connect("mqtt.hananet.co.kr", 8883)
        log.info("mqtt connected, loop_start()")

        mqtt.loop_start()
        
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
