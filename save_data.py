import serial
import csv
import time
import re

PORT = 'COM9'
BAUD_RATE = 115200
current_time = time.strftime("%Y%m%d_%H%M%S")
FILE_NAME = f"{current_time}_SMPS_Data.csv"

# found=1: new firmware format (save only found=1)
re_found1 = re.compile(
    r"\[FFT#(?P<seq>\d+)\]\s*found=1\s*"
    r"peak=(?P<peak_khz>[0-9.]+)kHz\s*"
    r"bin=(?P<bin>\d+)\s*"
    r"max=(?P<max>[0-9.]+)\s*"
    r"min=(?P<min>[0-9.]+)@(?P<min_khz>[0-9.]+)kHz\s*"
    r"vpk=(?P<vpk>[0-9.]+)V\s*"
    r"vpp=(?P<vpp>[0-9.]+)V\s*"
    r"vrms=(?P<vrms>[0-9.]+)V\s*"
    r"adc_pk=(?P<adc_pk>[0-9.]+)\s*"
    r"adc_pp=(?P<adc_pp>[0-9.]+)\s*"
    r"nf=(?P<nf>[0-9.]+)\s*"
    r"snr=(?P<snr>[0-9.]+)\s*"
    r"cnt=(?P<cnt>\d+)\s*"
    r"dt=(?P<dt_ms>[0-9.]+)ms\s*"
    r"fs_eff=(?P<fs_eff_k>[0-9.]+)k\s*"
    r"fs=(?P<fs_k>[0-9.]+)k\s*"
    r"nyq=(?P<nyq_k>[0-9.]+)k\s*"
    r"(?:lo=(?P<lo_k>[0-9.]+)k\s*)?"
    r"(?:hi=(?P<hi_k>[0-9.]+)k\s*)?"
    r"vin=(?P<vin>[0-9.]+)(?:V)?"
    r"(?:\s*i=(?P<i>[0-9.]+)(?:A)?)?"
)

try:
    ser = serial.Serial(PORT, BAUD_RATE, timeout=1)
    print(f"연결 성공: {PORT} 모니터링 중...")
    print(f"저장 파일명: {FILE_NAME}")

    with open(FILE_NAME, 'w', newline='') as f:
        writer = csv.writer(f)

        # ✅ 헤더 확장
        writer.writerow([
            "Timestamp", "FFT_Seq", "Found",
            "Freq(kHz)", "Bin", "Max", "Min", "MinFreq(kHz)",
            "Vpk(V)", "Vpp(V)", "Vrms(V)",
            "ADC_pk(counts)", "ADC_pp(counts)",
            "NF", "SNR", "Cnt",
            "Dt(ms)", "Fs_eff(k)", "Fs(k)", "Nyq(k)",
            "Current(A)", "Vin(V)"
        ])

        buffer = ""
        while True:
            if ser.in_waiting:
                raw_line = ser.readline().decode('utf-8', errors='ignore').strip()

                if "[FFT#" not in raw_line:
                    continue

                buffer += raw_line + "\n"
                ts = time.strftime("%Y-%m-%d %H:%M:%S")

                last_end = 0
                matched_any = False
                for m1 in re_found1.finditer(buffer):
                    matched_any = True
                    last_end = m1.end()

                    seq   = int(m1.group("seq"))
                    found = 1

                    peak  = float(m1.group("peak_khz"))

                    bin_idx = int(m1.group("bin"))
                    max_v   = float(m1.group("max"))
                    min_v   = float(m1.group("min"))
                    min_khz = float(m1.group("min_khz"))

                    vpk   = float(m1.group("vpk"))
                    vpp   = float(m1.group("vpp"))
                    vrms  = float(m1.group("vrms"))

                    adc_pk = float(m1.group("adc_pk"))
                    adc_pp = float(m1.group("adc_pp"))

                    nf     = float(m1.group("nf"))
                    snr     = float(m1.group("snr"))
                    cnt     = int(m1.group("cnt"))

                    dt_ms   = float(m1.group("dt_ms"))
                    fs_eff  = float(m1.group("fs_eff_k"))
                    fs_k    = float(m1.group("fs_k"))
                    nyq_k   = float(m1.group("nyq_k"))

                    vin   = float(m1.group("vin"))
                    cur_g = m1.group("i")
                    cur   = float(cur_g) if cur_g else ""

                    print(
                        f"[{ts}] seq={seq} found=1 peak={peak}kHz "
                        f"vpp={vpp} vrms={vrms} snr={snr} Vin={vin} I={cur}"
                    )

                    writer.writerow([
                        ts, seq, found,
                        peak, bin_idx, max_v, min_v, min_khz,
                        vpk, vpp, vrms,
                        adc_pk, adc_pp,
                        nf, snr, cnt,
                        dt_ms, fs_eff, fs_k, nyq_k,
                        cur, vin
                    ])
                    f.flush()

                if matched_any:
                    buffer = buffer[last_end:]
                elif len(buffer) > 4096:
                    buffer = buffer[-2048:]
                    print(f"파싱 실패 | 원문: {raw_line}")

except KeyboardInterrupt:
    print("\n중단됨. 저장 완료.")
finally:
    if 'ser' in locals():
        ser.close()
