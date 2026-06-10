import os
import sys
import time
from gpiozero import DigitalInputDevice, DigitalOutputDevice, Device
from gpiozero.pins.pigpio import PiGPIOFactory

Device.pin_factory = PiGPIOFactory()

PIN_ADT_PWR_IN = 24
PIN_ADT_5V_EN = 22
PIN_BAT_5V_EN = 10

BACKUP_FLAG_FILE = "/tmp/backup_done"  # 호환용: 직접 종료 모드에서는 사용하지 않음
FORCE_BACKUP_DONE_ON_START = False       # 호환용: 직접 종료 모드에서는 사용하지 않음
ALLOW_SHUTDOWN = False                   # True면 shutdown 명령 후 BAT 차단
HOLD_OFF_S = 2.0                         # 순간 노이즈 방지용: OFF 지속 확인 시간
RUN_BACKUP_BEFORE_POWEROFF = False       # 인증용: 백업 완료 플래그 대기 없이 바로 종료

BACKUP_SCRIPT = "/home/pi/backup.py"     # RUN_BACKUP_BEFORE_POWEROFF=True일 때만 사용
PYTHON_BIN = sys.executable

# GPIO24 raw 값이 1이면 어댑터 연결 상태로 해석
pwr_in = DigitalInputDevice(PIN_ADT_PWR_IN, pull_up=False, bounce_time=0.05)
adt_en = DigitalOutputDevice(PIN_ADT_5V_EN, active_high=True, initial_value=True)
bat_en = DigitalOutputDevice(PIN_BAT_5V_EN, active_high=True, initial_value=True)

# 직접 종료 모드에서는 /tmp/backup_done 파일을 삭제하거나 기다리지 않음

power_loss_detected = False
last_adapter_state = None


def raw_pwr_in() -> int:
    return 1 if pwr_in.value else 0


def is_adapter_on() -> bool:
    return raw_pwr_in() == 1


def log_state(prefix=""):
    raw = raw_pwr_in()
    adapter_on = is_adapter_on()
    print(
        f"[STATE{prefix} RAW_ADT_PWR_IN={raw} "
        f"ADT_PWR_IN={'H' if raw else 'L'} "
        f"ADAPTER={'ON' if adapter_on else 'OFF'} "
        f"ADT_5V_EN={'H' if adt_en.value else 'L'} "
        f"BAT_5V_EN={'H' if bat_en.value else 'L'}",
        flush=True,
    )



def apply_mode(adapter_on: bool):
    if adapter_on:
        adt_en.on()
        bat_en.on()
        log_state("(Adapter: ON)")
    else:
        adt_en.off()
        bat_en.on()
        log_state("(Adapter: OFF -> Battery)")



def is_backup_done() -> bool:
    """호환용 함수. 직접 종료 모드에서는 항상 True 처리."""
    return True



def on_power_in_rising():
    global power_loss_detected
    power_loss_detected = False
    apply_mode(adapter_on=True)



def on_power_in_falling():
    global power_loss_detected
    power_loss_detected = True
    apply_mode(adapter_on=False)
    if RUN_BACKUP_BEFORE_POWEROFF:
        print("[EVENT] power falling -> start backup", flush=True)
        os.system(f"{PYTHON_BIN} {BACKUP_SCRIPT} &")
    else:
        print("[EVENT] power falling -> direct poweroff mode", flush=True)



def cut_battery_poweroff():
    print("[ACTION] Adapter OFF confirmed. System Off", flush=True)
    if ALLOW_SHUTDOWN:
        os.system("sudo shutdown -h now")
        time.sleep(8)

    bat_en.off()
    log_state("(OFF BAT)")
    time.sleep(0.5)



def main():
    global last_adapter_state

    print("[BOOT] factory:", type(Device.pin_factory).__name__, flush=True)
    print(
        f"[BOOT] PIN_ADT_PWR_IN={PIN_ADT_PWR_IN}, raw={raw_pwr_in()}, adapter_on={is_adapter_on()}",
        flush=True,
    )

    current_adapter = is_adapter_on()
    last_adapter_state = current_adapter
    apply_mode(adapter_on=current_adapter)

    print("[INFO] 전원 시나리오 제어 시작. Ctrl+C로 종료.", flush=True)
    print("[INFO] 직접 종료 모드: /tmp/backup_done 대기 없음", flush=True)
    print(
        f"[INFO] ALLOW_SHUTDOWN={ALLOW_SHUTDOWN}, HOLD_OFF_S={HOLD_OFF_S}, "
        f"RUN_BACKUP_BEFORE_POWEROFF={RUN_BACKUP_BEFORE_POWEROFF}",
        flush=True,
    )

    try:
        while True:
            current_adapter = is_adapter_on()

            if current_adapter != last_adapter_state:
                if current_adapter:
                    on_power_in_rising()
                else:
                    on_power_in_falling()
                last_adapter_state = current_adapter

            if not current_adapter:
                t0 = time.monotonic()
                while (not is_adapter_on()) and (time.monotonic() - t0) < HOLD_OFF_S:
                    time.sleep(0.05)

                if (not is_adapter_on()) and power_loss_detected:
                    cut_battery_poweroff()
                    time.sleep(1)

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n[INFO] 종료", flush=True)
    finally:
        pass


if __name__ == "__main__":
    main()
