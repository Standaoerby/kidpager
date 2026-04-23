#!/bin/bash
# KidPager power-saving config, applied once at boot by kidpager-power.service.
#
# Wi-Fi policy is driven by /var/lib/kidpager/deploy-window (the "deploy flag"):
#
#   * absent (default / field mode)
#       Wi-Fi is rfkill-blocked on boot -> pager draws minimum power, no SSH.
#       Operator re-enables manually with Alt+W on the M4 keyboard.
#
#   * present (deploy mode, set by main.py before a UI-triggered reboot)
#       Wi-Fi stays UP for the number of seconds listed in the file (default
#       600 s / 10 min). A transient systemd-run timer fires at the end of
#       the window and re-blocks the radio, so the pager falls back into
#       field mode automatically if the operator forgets. The flag is
#       consumed (deleted) as soon as it is read, so a subsequent manual
#       reboot boots cleanly into field mode.
#
# The deploy-flag path exists because the pager lives in powersave mode in the
# field (Wi-Fi blocked), but the operator still needs a window to redeploy
# without first power-cycling or holding the M4 down for Alt+W. The UI reboot
# button in the profile menu writes the flag for us.
#
# Idempotent - safe to re-run. Each step guarded with -w / || true so a
# missing kernel feature on one Pi model never blocks the others from applying.
#
# Estimated savings on Pi Zero 2 W @ 600 MHz idle:
#   rfkill wifi        ~40-60 mA
#   powersave governor ~20-40 mA (caps freq at min, no boost)
#   ACT LED off        ~0.5-1 mA (plus no distracting blink)

set +e

DEPLOY_FLAG=/var/lib/kidpager/deploy-window
DEFAULT_WINDOW_SEC=600   # 10 min - covers a two-pager sequential redeploy
                         # with headroom. Tune by writing a different number
                         # to the flag file.

# 1. Wi-Fi policy - see header.
if [ -f "$DEPLOY_FLAG" ]; then
    # tr -dc '0-9' sanitises the input: we only trust digits, so a corrupt
    # flag file (stray newline, BOM, whatever) can't feed malformed --on-active.
    WINDOW_SEC=$(tr -dc '0-9' < "$DEPLOY_FLAG" 2>/dev/null)
    rm -f "$DEPLOY_FLAG"
    [ -z "$WINDOW_SEC" ] && WINDOW_SEC=$DEFAULT_WINDOW_SEC

    # Skipping the rfkill block is NOT enough on its own. NetworkManager
    # persists WirelessEnabled=false in /var/lib/NetworkManager/NetworkManager.state
    # once rfkill block caused it to flip, and that state survives reboots -
    # so NM starts this boot with its internal radio already off, and a
    # kernel rfkill that's merely "not blocked" doesn't flip it back on.
    # We have to mirror the ON path from power.wifi_toggle: rfkill unblock
    # -> nmcli radio on (flips WirelessEnabled persistent state) -> nmcli
    # device connect to force NM to attach the saved profile even if
    # autoconnect gave up earlier.
    #
    # CRITICAL: run the NM dance in a transient service via systemd-run
    # rather than inline. `nmcli device connect wlan0` blocks up to 90 s
    # waiting for association by default, and this script is a oneshot
    # ordered Before=kidpager.service -- inline blocking meant the pager's
    # UI came back up to 90 s late after a UI-triggered reboot, which
    # looks like "the pager didn't boot" to an impatient operator.
    # systemd-run --unit decouples: we queue the unit, return immediately,
    # kidpager.service starts right after, the NM dance runs in parallel.
    # --wait 15 caps the nmcli retry at 15 s so the transient can't wedge
    # either.
    systemd-run --unit=kidpager-wifi-open --no-block /bin/bash -c '
        for _ in $(seq 1 20); do
            nmcli -t general status >/dev/null 2>&1 && break
            sleep 0.5
        done
        rfkill unblock wifi 2>/dev/null
        nmcli radio wifi on 2>/dev/null
        nmcli --wait 15 device connect wlan0 2>/dev/null
    ' >/dev/null 2>&1

    # Close the window via a transient systemd-run timer. Mirrors the
    # "off" path in power.wifi_toggle so NM and rfkill go down in
    # lockstep - otherwise the W badge could get stuck on after close,
    # or NM could fight rfkill.
    systemd-run --on-active="${WINDOW_SEC}s" --unit=kidpager-wifi-close \
        /bin/bash -c 'nmcli radio wifi off 2>/dev/null; rfkill block wifi 2>/dev/null' \
        >/dev/null 2>&1
    echo "kidpager-power: deploy window active (${WINDOW_SEC}s), Wi-Fi UP (NM forced on, async)"
else
    # Field mode. Block rfkill; NM observes and updates its persistent
    # WirelessEnabled=false for us so the next field boot is consistent
    # without any work from this script.
    rfkill block wifi 2>/dev/null
fi

# 2. Pin every CPU core's cpufreq governor to powersave.
for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -w "$f" ] && echo powersave > "$f"
done

# 3. Disable the ACT LED heartbeat trigger. "ACT" is the modern kernel name,
# "led0" is the legacy one - try both, only one exists on a given kernel.
for led in /sys/class/leds/ACT/trigger /sys/class/leds/led0/trigger; do
    [ -w "$led" ] && echo none > "$led"
done

exit 0
