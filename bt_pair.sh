#!/bin/bash
# KidPager BT Pair -- pair, trust, connect keyboard.
#
# CRITICAL RULE: every bluetoothctl action that touches the bonding
# state machine MUST run inside ONE bluetoothctl session with the SSP
# agent active. Pair / trust / connect as separate `bluetoothctl pair
# ADDR` calls report "Pairing successful" but the LinkKey never gets
# written to /var/lib/bluetooth/<ctrl>/<dev>/info, so reconnection
# after reboot silently fails with "No link key" in the kernel log.
# All actions that mutate bonding are therefore funnelled through
# heredoc-piped bluetoothctl sessions that start with `power on` +
# `agent NoInputNoOutput` + `default-agent`.
#
# Verification: after the pair session we grep for `[LinkKey]` in the
# per-device info file. No LinkKey = silent-fail pairing; we tell the
# user to retry.
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

echo "[2] Scanning 20 sec -- PUT KEYBOARD IN PAIRING MODE..."
# Single bluetoothctl session with the NoInputNoOutput agent armed.
# The M4 keyboard has no display/keypad for PIN entry, so this is the
# matching IO capability (0x03). Running `scan on` inside this session
# (rather than as a backgrounded `bluetoothctl --timeout 20 scan on &`)
# keeps the agent alive in case the controller tries to auto-pair on
# discovery.
{
    echo "power on";              sleep 1
    echo "agent NoInputNoOutput"; sleep 1
    echo "default-agent";         sleep 1
    echo "scan on";               sleep 20
    echo "scan off";              sleep 1
    echo "quit"
} | bluetoothctl >/dev/null

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

echo "[4] Single session: pair -> trust -> connect..."
# pair + trust + connect in one agent-active session. See the file
# header comment for why this matters: split sessions look like they
# work (all commands return 0) but the LinkKey doesn't persist.
{
    echo "power on";              sleep 1
    echo "agent NoInputNoOutput"; sleep 1
    echo "default-agent";         sleep 1
    echo "pair $KB";              sleep 6
    echo "trust $KB";             sleep 2
    echo "connect $KB";           sleep 5
    echo "info $KB";              sleep 1
    echo "quit"
} | bluetoothctl

sleep 2

# Retry the connect if it didn't stick the first time. Don't retry
# pair/trust -- if those didn't work in the session above, pairing is
# genuinely broken and re-running without PIN input wouldn't help.
if bluetoothctl info "$KB" 2>/dev/null | grep -q "Connected: yes"; then
    echo "=== CONNECTED ==="
else
    echo "  Not connected yet, retrying..."
    { echo "connect $KB"; sleep 5; echo "quit"; } | bluetoothctl
    sleep 2
fi

echo ""
echo "[5] Status:"
echo "  Paired:    $(bluetoothctl info "$KB" 2>/dev/null | grep Paired)"
echo "  Trusted:   $(bluetoothctl info "$KB" 2>/dev/null | grep Trusted)"
echo "  Bonded:    $(bluetoothctl info "$KB" 2>/dev/null | grep Bonded)"
echo "  Connected: $(bluetoothctl info "$KB" 2>/dev/null | grep Connected)"

# LinkKey persistence check. This is the ground-truth test for whether
# pairing actually survives a reboot. `Bonded: yes` from bluetoothctl
# info only reflects the in-memory state of bluetoothd; if the kernel
# LinkKey record wasn't written, the next boot will start with no
# saved key and HID will never re-attach.
INFO_FILE=$(ls /var/lib/bluetooth/*/"$KB"/info 2>/dev/null | head -1)
if [ -n "$INFO_FILE" ] && grep -q "\[LinkKey\]" "$INFO_FILE" 2>/dev/null; then
    echo "  LinkKey:   saved -- reconnection will survive reboots"
else
    echo "  LinkKey:   NOT saved -- pairing will NOT survive reboot"
    echo "             Re-run this script; if it still fails, reset the"
    echo "             M4 (power off then hold Fn+connect to re-enter"
    echo "             pairing mode) and retry."
fi

echo ""
echo "Input devices:"
ls /dev/input/event* 2>/dev/null | sed 's/^/  /' || echo "  (none)"
echo ""

read -p "Start KidPager? [Y/n] " YN
if [ "$YN" != "n" ] && [ "$YN" != "N" ]; then
    systemctl start kidpager 2>/dev/null || (cd /home/pi/kidpager && python3 main.py)
fi
