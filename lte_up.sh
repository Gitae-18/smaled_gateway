#!/bin/bash
set -e

MODEM=0

echo "[LTE_UP] starting (auto-detect data bearer)."

# 모뎀 정보에서 bearer 리스트 가져오기
INFO_M=$(mmcli -m "$MODEM" --output-keyvalue)

# 예: modem.bearers=/org/.../Bearer/0,/org/.../Bearer/1
BEARERS_LINE=$(echo "$INFO_M" | awk -F= '/modem.bearers/ {print $2}')

if [ -z "$BEARERS_LINE" ]; then
    echo "[LTE_UP] no bearers found on modem $MODEM"
    echo "$INFO_M"
    exit 1
fi

# 쉼표로 구분된 path들을 공백으로 바꿔서 for 루프로 돌림
BEARERS=$(echo "$BEARERS_LINE" | tr ',' ' ')

DATA_IF=""
DATA_ADDR=""
DATA_PFX=""
DATA_GW=""
DATA_DNS1=""
DATA_DNS2=""
DATA_BID=""

for path in $BEARERS; do
    BID=${path##*/}  # .../Bearer/0 -> 0
    INFO_B=$(mmcli -b "$BID" --output-keyvalue)

    ADDR=$(echo "$INFO_B" | awk -F= '/ipv4.address/ {print $2}')
    PFX=$(echo  "$INFO_B" | awk -F= '/ipv4.prefix/ {print $2}')
    GW=$(echo   "$INFO_B" | awk -F= '/ipv4.gateway/ {print $2}')
    IFACE=$(echo "$INFO_B" | awk -F= '/bearer.interface/ {print $2}')
    DNS1=$(echo "$INFO_B" | awk -F= '/ipv4.dns1/ {print $2}')
    DNS2=$(echo "$INFO_B" | awk -F= '/ipv4.dns2/ {print $2}')

    # ipv4.address가 있는 bearer를 데이터 bearer로 사용
    if [ -n "$ADDR" ] && [ -n "$PFX" ] && [ -n "$GW" ] && [ -n "$IFACE" ]; then
        DATA_IF="$IFACE"
        DATA_ADDR="$ADDR"
        DATA_PFX="$PFX"
        DATA_GW="$GW"
        DATA_DNS1="$DNS1"
        DATA_DNS2="$DNS2"
        DATA_BID="$BID"
        break
    fi
done

if [ -z "$DATA_IF" ]; then
    echo "[LTE_UP] no bearer with IPv4 config found."
    echo "[LTE_UP] modem info:"
    echo "$INFO_M"
    exit 1
fi

echo "[LTE_UP] using bearer $DATA_BID on interface $DATA_IF"
echo "[LTE_UP] addr=$DATA_ADDR/$DATA_PFX gw=$DATA_GW"

# 인터페이스 활성화 + IP/라우트 설정
ip link set "$DATA_IF" up || true
ip addr flush dev "$DATA_IF" || true
ip addr add "$DATA_ADDR"/"$DATA_PFX" dev "$DATA_IF"

# 기본 라우트 추가 (metric은 환경에 맞게 조정)
ip route add default via "$DATA_GW" dev "$DATA_IF" metric 100 || true

echo "[LTE_UP] current addresses on $DATA_IF:"
ip addr show "$DATA_IF"

# DNS 설정
if [ -n "$DATA_DNS1" ] || [ -n "$DATA_DNS2" ]; then
    echo "[LTE_UP] setting DNS to $DATA_DNS1 $DATA_DNS2"
    {
        [ -n "$DATA_DNS1" ] && echo "nameserver $DATA_DNS1"
        [ -n "$DATA_DNS2" ] && echo "nameserver $DATA_DNS2"
    } > /etc/resolv.conf
fi

echo "[LTE_UP] done."
exit 0
