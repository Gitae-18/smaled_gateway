#include <stdio.h>
#include <pigpio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>

#define DEVICE_SENSOR "/dev/ttyAMA3"
#define DEVICE_H2S    "/dev/ttyAMA5"  /* optional, retained for compatibility */
#define SENSOR_BAUD 9600
#define SENSOR_FRAME_SIZE 26
#define ZPHS01B_WARMUP_SEC 180
#define SENSOR_RX_TIMEOUT_MS 2000
#define SENSOR_ERROR_VALUE (-999.0)

#define RS485_ENABLE_PIN 7
#define LIS3DH_I2C_ADDR 0x19
#define I2C_BUS 1
#define LIS3DH_RANGE 0.001 /* Preserves the original scaling */

static int lis3dh_handle = -1;

static double monotonic_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void print_sensor_frame(const uint8_t *buf, size_t len)
{
    printf("[ENV RAW]");
    for (size_t i = 0; i < len; ++i) printf(" %02X", buf[i]);
    putchar('\n');
}

static bool init_lis3dhtr(void)
{
    lis3dh_handle = i2cOpen(I2C_BUS, LIS3DH_I2C_ADDR, 0);
    if (lis3dh_handle < 0) {
        fprintf(stderr, "[LIS3DH] I2C open failed: %d\n", lis3dh_handle);
        return false;
    }
    if (i2cWriteByteData(lis3dh_handle, 0x20, 0x57) < 0 ||
        i2cWriteByteData(lis3dh_handle, 0x23, 0x00) < 0) {
        fprintf(stderr, "[LIS3DH] register setup failed\n");
        return false;
    }
    time_sleep(0.1);
    return true;
}

static bool read_lis3dhtr_axes(double *x, double *y, double *z)
{
    *x = *y = *z = SENSOR_ERROR_VALUE;
    if (lis3dh_handle < 0) return false;
    int xl = i2cReadByteData(lis3dh_handle, 0x28);
    int xh = i2cReadByteData(lis3dh_handle, 0x29);
    int yl = i2cReadByteData(lis3dh_handle, 0x2A);
    int yh = i2cReadByteData(lis3dh_handle, 0x2B);
    int zl = i2cReadByteData(lis3dh_handle, 0x2C);
    int zh = i2cReadByteData(lis3dh_handle, 0x2D);
    if (xl < 0 || xh < 0 || yl < 0 || yh < 0 || zl < 0 || zh < 0) return false;
    int16_t raw_x = (int16_t)((xh << 8) | xl) >> 4;
    int16_t raw_y = (int16_t)((yh << 8) | yl) >> 4;
    int16_t raw_z = (int16_t)((zh << 8) | zl) >> 4;
    *x = raw_x * LIS3DH_RANGE;
    *y = raw_y * LIS3DH_RANGE;
    *z = raw_z * LIS3DH_RANGE;
    return true;
}

/* ZPHS01B 26-byte reply: 0=0xFF, 1=0x86, 25=checksum.
 * Checksum = two's complement of sum of bytes 1 through 24 (8-bit).
 */
static uint8_t zphs01b_checksum(const uint8_t frame[SENSOR_FRAME_SIZE])
{
    uint8_t sum = 0;
    for (int i = 1; i <= 24; ++i) sum = (uint8_t)(sum + frame[i]);
    return (uint8_t)(0u - sum);
}

static bool zphs01b_frame_valid(const uint8_t frame[SENSOR_FRAME_SIZE])
{
    if (frame[0] != 0xFF || frame[1] != 0x86) {
        fprintf(stderr, "[ENV] HEADER FAIL: %02X %02X\n", frame[0], frame[1]);
        return false;
    }
    uint8_t expected = zphs01b_checksum(frame);
    if (expected != frame[25]) {
        fprintf(stderr, "[ENV] CHECKSUM FAIL expected=0x%02X received=0x%02X\n",
                expected, frame[25]);
        return false;
    }
    return true;
}

/* Read an aligned reply; avoid interpreting intermittent 'no data' as bytes.
 * The timeout covers the complete 26-byte response. */
static bool zphs01b_read_frame(int fd, uint8_t frame[SENSOR_FRAME_SIZE])
{
    int pos = 0;
    const double deadline = monotonic_seconds() + SENSOR_RX_TIMEOUT_MS / 1000.0;
    while (monotonic_seconds() < deadline) {
        int rx = serReadByte(fd);
        if (rx < 0) {
            time_sleep(0.005);
            continue;
        }
        uint8_t b = (uint8_t)rx;
        if (pos == 0) {
            if (b != 0xFF) continue;
        } else if (pos == 1 && b != 0x86) {
            pos = (b == 0xFF) ? 1 : 0;
            if (pos == 1) frame[0] = 0xFF;
            continue;
        }
        frame[pos++] = b;
        if (pos == SENSOR_FRAME_SIZE) return true;
    }
    fprintf(stderr, "[ENV] RX TIMEOUT (%d/%d bytes)\n", pos, SENSOR_FRAME_SIZE);
    return false;
}

static uint16_t be16(const uint8_t *data, int high_index)
{
    return (uint16_t)(((uint16_t)data[high_index] << 8) | data[high_index + 1]);
}

int main(void)
{
    if (gpioInitialise() < 0) {
        fprintf(stderr, "Unable to initialize pigpio\n");
        return 1;
    }
    gpioSetMode(RS485_ENABLE_PIN, PI_OUTPUT);
    gpioWrite(RS485_ENABLE_PIN, 0);

    int sensor_fd = serOpen(DEVICE_SENSOR, SENSOR_BAUD, 0);
    if (sensor_fd < 0) {
        fprintf(stderr, "[ENV] Unable to open %s: %d\n", DEVICE_SENSOR, sensor_fd);
        gpioTerminate();
        return 1;
    }
    /* H2S is optional and unused in the present gw_env payload. */
    int h2s_fd = serOpen(DEVICE_H2S, SENSOR_BAUD, 0);
    if (h2s_fd < 0) fprintf(stderr, "[ENV] Optional H2S UART unavailable: %s\n", DEVICE_H2S);

    if (!init_lis3dhtr()) fprintf(stderr, "[ENV] accelerometer unavailable (-999 will be emitted)\n");

    const uint8_t command[] = {0xFF, 0x01, 0x86, 0, 0, 0, 0, 0, 0x79};
    const double warmup_start = monotonic_seconds();
    bool warmup_announced = false;
    int last_reported_remaining = -1;
    fprintf(stderr, "[ENV] ZPHS01B warmup started: %d sec\n", ZPHS01B_WARMUP_SEC);

    while (1) {
        double temp = SENSOR_ERROR_VALUE, humi = SENSOR_ERROR_VALUE;
        double pm25 = SENSOR_ERROR_VALUE, pm10 = SENSOR_ERROR_VALUE, pm1 = SENSOR_ERROR_VALUE;
        double co2 = SENSOR_ERROR_VALUE, voc = SENSOR_ERROR_VALUE, ch2o = SENSOR_ERROR_VALUE;
        double co = SENSOR_ERROR_VALUE, o3 = SENSOR_ERROR_VALUE, no2 = SENSOR_ERROR_VALUE;

        double elapsed = monotonic_seconds() - warmup_start;
        if (elapsed >= ZPHS01B_WARMUP_SEC) {
            if (!warmup_announced) {
                fprintf(stderr, "[ENV] ZPHS01B warmup complete; sampling enabled\n");
                warmup_announced = true;
            }
            uint8_t frame[SENSOR_FRAME_SIZE] = {0};
            /* Remove stale bytes left from previous unsuccessful reads. */
            int avail = serDataAvailable(sensor_fd);
            if (avail > 0) {
                for (int i = 0; i < avail; ++i) {
                    if (serReadByte(sensor_fd) < 0) break;
                }
            }
            if (serWrite(sensor_fd, (char *)command, sizeof(command)) < 0) {
                fprintf(stderr, "[ENV] request TX failed\n");
            } else if (zphs01b_read_frame(sensor_fd, frame)) {
                print_sensor_frame(frame, sizeof(frame));
                if (zphs01b_frame_valid(frame)) {
                    temp = ((int)be16(frame, 11) - 500) * 0.1;
                    humi = be16(frame, 13);
                    pm25 = be16(frame, 4) / 10.0;
                    pm10 = be16(frame, 6) / 10.0;
                    pm1 = be16(frame, 2) / 10.0;
                    co2 = be16(frame, 8);
                    voc = frame[10];
                    ch2o = be16(frame, 15) * 0.0001 / 10.0; /* preserve existing scaling */
                    co = be16(frame, 17) * 0.1;
                    o3 = be16(frame, 19) * 0.01;
                    no2 = be16(frame, 21) * 0.01;
                    printf("[ENV GAS RAW] O3_H=%u O3_L=%u O3=%.2f ppm, NO2_H=%u NO2_L=%u NO2=%.2f ppm\n",
                           frame[19], frame[20], o3, frame[21], frame[22], no2);
                } else {
                    fprintf(stderr, "[ENV] invalid frame; publishing -999 environmental values\n");
                }
            }
        } else {
            int remaining = ZPHS01B_WARMUP_SEC - (int)elapsed;
            if (remaining != last_reported_remaining && (remaining % 30 == 0 || last_reported_remaining < 0)) {
                fprintf(stderr, "[ENV] ZPHS01B warmup: %d sec remaining\n", remaining);
                last_reported_remaining = remaining;
            }
        }

        double ax, ay, az;
        if (!read_lis3dhtr_axes(&ax, &ay, &az))
            fprintf(stderr, "[ENV] accelerometer read failed; publishing -999 values\n");

        time_t now = time(NULL);
        printf("{\"t\":\"gw_env\",\"ts\":%ld,"
               "\"temp\":%.2f,\"humi\":%.2f,"
               "\"pm1\":%.2f,\"pm25\":%.2f,\"pm10\":%.2f,"
               "\"co2\":%.0f,\"voc\":%.0f,"
               "\"ch2o\":%.4f,\"co\":%.1f,\"o3\":%.2f,\"no2\":%.2f,"
               "\"ax\":%.3f,\"ay\":%.3f,\"az\":%.3f}\n",
               (long)now, temp, humi, pm1, pm25, pm10, co2, voc,
               ch2o, co, o3, no2, ax, ay, az);
        fflush(stdout);
        time_sleep(1.0);
    }

    if (lis3dh_handle >= 0) i2cClose(lis3dh_handle);
    serClose(sensor_fd);
    if (h2s_fd >= 0) serClose(h2s_fd);
    gpioTerminate();
    return 0;
}
