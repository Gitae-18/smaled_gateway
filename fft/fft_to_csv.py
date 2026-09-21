import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import serial
from serial import SerialException


FFT_DATA_PREFIX = "[FFT_DATA]"

FIELD_ORDER = [
    "pc_timestamp",
    "elapsed_sec",
    "seq",
    "found",
    "freq_hz",
    "amp",
    "vin_v",
    "curr_a",
    "span",
    "fs_hz",    
    "vpk",
    "adc_pk",
    "fault_type",
    "smps_capacity_w",
    "device_id",
    "note",
]

KEY_VALUE_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)="
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
)


def parse_fft_data_line(line: str):
    """Parse one MCU '[FFT_DATA]' log line into a dictionary."""
    if not line.startswith(FFT_DATA_PREFIX):
        return None

    parsed = {key: value for key, value in KEY_VALUE_RE.findall(line)}

    required = {
        "seq",
        "found",
        "freq_hz",
        "amp",
        "vin_v",
        "curr_a",
        "span",
        "fs_hz",
        "vpk",
        "adc_pk",
    }

    if not required.issubset(parsed):
        return None

    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 값을 입력해야 합니다.")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("0 이상의 값을 입력해야 합니다.")
    return parsed


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="STM32 UART의 [FFT_DATA] 로그를 CSV로 저장합니다."
    )
    parser.add_argument("--port", default="COM9", help="시리얼 포트 (기본: COM9)")
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="보드레이트 (기본: 115200)",
    )
    parser.add_argument(
        "--duration",
        type=positive_float,
        default=60.0,
        help="수집 시간(초). 기본: 60",
    )
    parser.add_argument(
        "--output-dir",
        default="fft_csv",
        help="저장 폴더 (기본: fft_csv)",
    )
    parser.add_argument(
        "--fault-type",
        default="normal",
        help="상태 라벨 (기본: normal)",
    )
    parser.add_argument(
        "--smps-capacity-w",
        type=non_negative_float,
        default=0.0,
        help="SMPS 정격 용량(W). 미지정 시 0",
    )
    parser.add_argument(
        "--device-id",
        default="",
        help="단말 또는 조명 식별자",
    )
    parser.add_argument(
        "--note",
        default="",
        help="시험 조건 메모",
    )
    parser.add_argument(
        "--save-invalid",
        action="store_true",
        help="found=0 데이터도 CSV에 저장",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now()
    filename_stamp = started_at.strftime("%Y%m%d_%H%M%S")

    csv_path = output_dir / f"fft_data_{filename_stamp}.csv"
    raw_log_path = output_dir / f"uart_raw_{filename_stamp}.log"

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            timeout=1,
        )
    except SerialException as exc:
        print(f"[ERROR] 시리얼 포트를 열 수 없습니다: {exc}", file=sys.stderr)
        return 1

    saved_count = 0
    skipped_count = 0
    malformed_count = 0
    start_monotonic = time.monotonic()

    print(f"[START] port={args.port} baud={args.baud}")
    print(f"[START] duration={args.duration:.1f}s")
    print(f"[CSV] {csv_path.resolve()}")
    print(f"[RAW] {raw_log_path.resolve()}")

    try:
        with (
            csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file,
            raw_log_path.open("w", encoding="utf-8") as raw_file,
        ):
            writer = csv.DictWriter(csv_file, fieldnames=FIELD_ORDER)
            writer.writeheader()

            while (time.monotonic() - start_monotonic) < args.duration:
                raw = ser.readline()
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                raw_file.write(line + "\n")

                parsed = parse_fft_data_line(line)
                if parsed is None:
                    if line.startswith(FFT_DATA_PREFIX):
                        malformed_count += 1
                        print(f"[WARN] 파싱 실패: {line}")
                    continue

                found = int(float(parsed["found"]))
                if found == 0 and not args.save_invalid:
                    skipped_count += 1
                    continue

                now = datetime.now()
                elapsed_sec = time.monotonic() - start_monotonic

                row = {
                    "pc_timestamp": now.isoformat(timespec="milliseconds"),
                    "elapsed_sec": f"{elapsed_sec:.3f}",
                    "seq": int(float(parsed["seq"])),
                    "found": found,
                    "freq_hz": float(parsed["freq_hz"]),
                    "amp": float(parsed["amp"]),
                    "vin_v": float(parsed["vin_v"]),
                    "curr_a": float(parsed["curr_a"]),
                    "span": int(float(parsed["span"])),
                    "fs_hz": float(parsed["fs_hz"]),
                    "vpk": float(parsed["vpk"]),
                    "adc_pk": float(parsed["adc_pk"]),
                    "fault_type": args.fault_type,
                    "smps_capacity_w": args.smps_capacity_w,
                    "device_id": args.device_id,
                    "note": args.note,
                }

                writer.writerow(row)
                csv_file.flush()
                raw_file.flush()

                saved_count += 1

                if saved_count % 20 == 0:
                    print(
                        f"[PROGRESS] saved={saved_count} "
                        f"seq={row['seq']} "
                        f"freq={row['freq_hz']:.2f}Hz "
                        f"amp={row['amp']:.6f}"
                    )

    except KeyboardInterrupt:
        print("\n[STOP] 사용자 중지")
    except SerialException as exc:
        print(f"\n[ERROR] 시리얼 수신 오류: {exc}", file=sys.stderr)
        return 1
    finally:
        ser.close()

    elapsed_total = time.monotonic() - start_monotonic
    print(
        f"[DONE] elapsed={elapsed_total:.1f}s "
        f"saved={saved_count} "
        f"skipped_found0={skipped_count} "
        f"malformed={malformed_count}"
    )
    print(f"[DONE] CSV={csv_path.resolve()}")
    print(f"[DONE] RAW={raw_log_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
