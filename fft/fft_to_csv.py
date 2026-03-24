import serial
import csv
import time
from datetime import datetime
# 포트와 속도 설정
ser = serial.Serial('COM9', 9600, timeout=1)


for cycle in range(1):  # 1분 단위로 3번 저장 예시
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"fft_data_{timestamp}.csv"
    print(f"📂 시작: {filename}")

    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Frequency (Hz)', 'Amplitude'])

        start_time = time.time()

        while time.time() - start_time < 60:  # 1분 저장
            raw = ser.readline()
            try:
                line = raw.decode('utf-8', errors='ignore').strip()
            except UnicodeDecodeError:
                continue

            if line:
                parts = line.split(',')
                if len(parts) == 2:
                    writer.writerow(parts)

    print(f"✅ 저장 완료: {filename}")

ser.close()