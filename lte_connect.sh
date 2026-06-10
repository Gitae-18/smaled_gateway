#!/bin/bash
set -e

MODEM=0
APN="m2m-router.lguplus.co.kr"

echo "[LTE_CONNECT] starting..."

# 모뎀이 준비될 때까지 최대 60초 기다리기
mmcli -m "$MODEM" --wait-for-modem=60 || {
    echo "[LTE_CONNECT] modem $MODEM not ready."
    exit 1
}

echo "[LTE_CONNECT] modem $MODEM detected."

# 예전 bearer들 건드리지 말고, simple-connect만 수행
echo "[LTE_CONNECT] simple-connect (ipv4)..."
mmcli -m "$MODEM" --simple-connect="apn=$APN,ip-type=ipv4"

echo "[LTE_CONNECT] done."
exit 0
