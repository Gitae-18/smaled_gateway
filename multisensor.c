#include <stdio.h>
#include <pigpio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <sys/ioctl.h>

#define RS485_ENABLE_PIN 7
#define HIGH 1
#define LOW 0
#define ZPHS01B_ADDR 0xFF

#define DEVICE_SENSOR "/dev/ttyAMA3"  // 환경센서 (TX/RX)
#define DEVICE_H2S    "/dev/ttyAMA5"  // H2S 센서 (옵션)

#define LIS3DH_SCALE_2G 0.001  // 1mg/LSB
#define LIS3DH_SCALE_4G 0.002
#define LIS3DH_SCALE_8G 0.004
#define LIS3DH_SCALE_16G 0.012

#define LIS3DH_RANGE LIS3DH_SCALE_2G 

#define LIS3DH_I2C_ADDR 0x19
#define I2C_BUS 1
int lis3dh_handle = -1;

void init_lis3dhtr() {
    lis3dh_handle = i2cOpen(I2C_BUS, LIS3DH_I2C_ADDR, 0);
    if (lis3dh_handle < 0) {
        fprintf(stderr, "❌ LIS3DHTR I2C Open 실패\n");
        exit(1);
    }

    // CTRL_REG1 (0x20): 0x57 = 50Hz, all axis enabled
    i2cWriteByteData(lis3dh_handle, 0x20, 0x57);

    // CTRL_REG4 (0x23): ±2g full scale, High Resolution
    i2cWriteByteData(lis3dh_handle, 0x23, 0x00);

    time_sleep(0.1);  // 안정화 대기
}

void read_lis3dhtr_axes(double *x, double *y, double *z) {
    uint8_t xl, xh, yl, yh, zl, zh;
    xl = i2cReadByteData(lis3dh_handle, 0x28);
    xh = i2cReadByteData(lis3dh_handle, 0x29);
    yl = i2cReadByteData(lis3dh_handle, 0x2A);
    yh = i2cReadByteData(lis3dh_handle, 0x2B);
    zl = i2cReadByteData(lis3dh_handle, 0x2C);
    zh = i2cReadByteData(lis3dh_handle, 0x2D);

    int16_t raw_x = (int16_t)(xh << 8 | xl) >> 4;
    int16_t raw_y = (int16_t)(yh << 8 | yl) >> 4;
    int16_t raw_z = (int16_t)(zh << 8 | zl) >> 4;

    *x = raw_x * LIS3DH_RANGE;
    *y = raw_y * LIS3DH_RANGE;
    *z = raw_z * LIS3DH_RANGE;
}

int main() {
    if (gpioInitialise() < 0) {
        fprintf(stderr, "Unable to initialize pigpio\n");
        return 1;
    }

    gpioSetMode(RS485_ENABLE_PIN, PI_OUTPUT);

    int sensor_fd = serOpen(DEVICE_SENSOR, 9600, 0);
    int h2s_fd = serOpen(DEVICE_H2S, 9600, 0);
    if (sensor_fd < 0 || h2s_fd < 0) {
        fprintf(stderr, "Unable to open UART\n");
        return 1;
    }

    init_lis3dhtr();

    char command[] = { 0xFF, 0x01, 0x86, 0x00, 0x00, 0x00, 0x00, 0x00, 0x79 };

    while (1) {
        serWrite(sensor_fd, command, sizeof(command));
        time_sleep(0.5);

        unsigned char dataFromSensor[26];
        for (int i = 0; i < 26; i++) {
            dataFromSensor[i] = serReadByte(sensor_fd);
        }

        unsigned char receivedSensor[9];

        for (int i = 0; i < 9; i++) {
            receivedSensor[i] = serReadByte(h2s_fd);
        }

        double temp = ((dataFromSensor[11] * 256 + dataFromSensor[12]) - 500) * 0.1;
        double humi = dataFromSensor[13] * 256 + dataFromSensor[14];
        double pm25 = (dataFromSensor[4] * 256 + dataFromSensor[5]) / 10.0;
        double pm10 = (dataFromSensor[6] * 256 + dataFromSensor[7]) / 10.0;
        double pm1  = (dataFromSensor[2] * 256 + dataFromSensor[3]) / 10.0;
        double co2  = (dataFromSensor[8] * 256 + dataFromSensor[9]);
        double voc  = dataFromSensor[10];
        double ch2o = (dataFromSensor[15] * 256 + dataFromSensor[16]) * 0.0001 / 10.0;
        double co   = (dataFromSensor[17] * 256 + dataFromSensor[18]) * 0.1;
        double o3   = (dataFromSensor[19] * 256 + dataFromSensor[20]) * 0.1;
        double no2  = (dataFromSensor[21] * 256 + dataFromSensor[22]) * 0.1;
//        double h2s  = (receivedSensor[2] * 256 + receivedSensor[3]) / 10.0;

        

        printf("[ENV] Temp: %.2f\u00b0C, Humi: %.2f%%, PM2.5: %.2f, CO2: %.0f\n",
               temp, humi, pm25, co2);
        double ax, ay, az;
        read_lis3dhtr_axes(&ax, &ay, &az);
        printf("LIS3DHTR -> X: %.3fg, Y: %.3fg, Z: %.3fg\n", ax, ay, az);

        time_t now = time(NULL);
        printf(
            "{\"t\":\"gw_env\",\"ts\":%ld,"
            "\"temp\":%.2f,\"humi\":%.2f,"
            "\"pm1\":%.2f,\"pm25\":%.2f,\"pm10\":%.2f,"
            "\"co2\":%.0f,\"voc\":%.0f,"
            "\"ch2o\":%.4f,\"co\":%.1f,\"o3\":%.1f,\"no2\":%.1f,"
            "\"ax\":%.3f,\"ay\":%.3f,\"az\":%.3f}"
            "\n",
            (long)now,
            temp, humi,
            pm1, pm25, pm10,
            co2, voc,
            ch2o, co, o3, no2,
            ax, ay, az
        );
        fflush(stdout);
    }

    i2cClose(lis3dh_handle);
    serClose(sensor_fd);
    serClose(h2s_fd);
    gpioTerminate();
    return 0;
}
