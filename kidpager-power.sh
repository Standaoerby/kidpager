#!/bin/bash
# KidPager power-saving config, applied once at boot by kidpager-power.service.
#
# Idempotent — safe to re-run. Each step guarded with -w / || true so a
# missing kernel feature on one Pi model never blocks the others from applying.
#
# Estimated savings on Pi Zero 2 W @ 600 MHz idle:
#   rfkill wifi        ~40-60 mA
#   powersave governor ~20-40 mA (caps freq at min, no boost)
#   ACT LED off        ~0.5-1 mA (plus no distracting blink)
#
# Wi-Fi can be re-enabled on demand from the pager UI via Alt+W.

set +e

# 1. Block Wi-Fi. `rfkill` handles multiple radios; `wifi` targets only 802.11.
rfkill block wifi 2>/dev/null

# 2. Pin every CPU core's cpufreq governor to powersave.
for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -w "$f" ] && echo powersave > "$f"
done

# 3. Disable the ACT LED heartbeat trigger. "ACT" is the modern kernel name,
# "led0" is the legacy one — try both, only one exists on a given kernel.
for led in /sys/class/leds/ACT/trigger /sys/class/leds/led0/trigger; do
    [ -w "$led" ] && echo none > "$led"
done

exit 0
