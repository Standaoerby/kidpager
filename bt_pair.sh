#!/bin/bash
# KidPager BT Pair — pair, trust, connect keyboard
set -e
echo "=== BT Pair ==="

systemctl stop kidpager 2>/dev/null || true
killall python3 2>/dev/null || true
sleep 1

rfkill unblock bluetooth 2>/dev/null
systemctl restart bluetooth
sleep 2
hciconfig hci0 up 2>/dev/null
sleep 1

echo "[1] Removing old devices..."
for DEV in $(bluetoothctl devices 2>/dev/null | awk '{print $2}'); do
    bluetoothctl remove "$DEV" 2>/dev/null || true
done

echo "[2] Scanning 20 sec — PUT KEYBOARD IN PAIRING MODE..."
bluetoothctl --timeout 20 scan on 2>/dev/null &
sleep 20
kill %1 2>/dev/null || true
sleep 1

echo "[3] Looking for keyboard..."
KB=""
while IFS= read -r line; do
    ADDR=$(echo "$line" | awk '{print $2}')
    NAME=$(echo "$line" | cut -d' ' -f3-)
    UP=$(echo "$NAME" | tr '[:lower:]' '[:upper:]')
    if echo "$UP" | grep -qE "M4|KEYBOARD|KB|BT-KEY|HID"; then
        KB="$ADDR"
        echo "  Found: $NAME ($ADDR)"
        break
    fi
done <<< "$(bluetoothctl devices 2>/dev/null)"

if [ -z "$KB" ]; then
    echo "  No keyboard found. All devices:"
    bluetoothctl devices 2>/dev/null
    read -p "  Enter address: " KB
fi

echo "[4] Pair -> Trust -> Connect..."
bluetoothctl pair "$KB"
sleep 3
bluetoothctl trust "$KB"
sleep 1
bluetoothctl connect "$KB"
sleep 3

if bluetoothctl info "$KB" 2>/dev/null | grep -q "Connected: yes"; then
    echo "=== CONNECTED ==="
else
    echo "  Retry connect..."
    bluetoothctl connect "$KB"
    sleep 3
fi

echo ""
echo "Paired: $(bluetoothctl info "$KB" 2>/dev/null | grep Paired)"
echo "Trusted: $(bluetoothctl info "$KB" 2>/dev/null | grep Trusted)"
echo "Connected: $(bluetoothctl info "$KB" 2>/dev/null | grep Connected)"
echo ""

read -p "Start KidPager? [Y/n] " YN
if [ "$YN" != "n" ] && [ "$YN" != "N" ]; then
    systemctl start kidpager 2>/dev/null || (cd /home/pi/kidpager && python3 main.py)
fi
