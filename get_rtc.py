import os
import fcntl
import struct
import datetime

# RTC_RD_TIME ioctl 코드 (from <linux/rtc.h>)
RTC_RD_TIME = 0x80247009

def read_rtc_via_ioctl(rtc_dev="/dev/rtc0"):
    fmt = "9i"
    size = struct.calcsize(fmt)

    with open(rtc_dev, "rb") as fd:
        buf = fcntl.ioctl(fd, RTC_RD_TIME, b'\x00' * size)
        tm = struct.unpack(fmt, buf)
        # C struct tm_mon is 0-11, C tm_year is years since 1900
        year = tm[5] + 1900
        month = tm[4] + 1
        return datetime.datetime(year, month, tm[3], tm[2], tm[1], tm[0])

if __name__ == "__main__":
    rtc_time = read_rtc_via_ioctl()
    print("RTC time:", rtc_time)
