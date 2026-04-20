#!/usr/bin/env python3
"""Verify KidPager power-saving config is active.

Run:  sudo python3 test_power.py
Exits 0 if everything is as expected, 1 otherwise.
"""
import subprocess, os, sys


def cat(path, default="<missing>"):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception as e:
        return f"<error: {e}>"


def sh(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def check(ok, label, detail=""):
    mark = " OK " if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"  — {detail}"
    print(line)
    return ok


def main():
    fails = 0
    print("=== KidPager power config check ===\n")

    # 1. kidpager-power.service oneshot must have run and remained active.
    _, out, _ = sh(["systemctl", "is-active", "kidpager-power"])
    if not check(out == "active", "kidpager-power.service active", out):
        fails += 1

    # 2. Wi-Fi rfkill: expected blocked after boot (user unblocks with Alt+W).
    _, out, _ = sh(["rfkill", "list", "wifi"])
    blocked = "Soft blocked: yes" in out
    if not check(blocked, "Wi-Fi soft-blocked at boot",
                 "Alt+W on pager toggles at runtime"):
        fails += 1

    # 3. CPU governor: every core must be 'powersave'.
    governors = []
    cpu_root = "/sys/devices/system/cpu"
    try:
        for name in sorted(os.listdir(cpu_root)):
            gov_path = f"{cpu_root}/{name}/cpufreq/scaling_governor"
            if os.path.isfile(gov_path):
                governors.append((name, cat(gov_path)))
    except Exception as e:
        governors = [("<error>", str(e))]
    all_powersave = bool(governors) and all(g == "powersave" for _, g in governors)
    detail = ", ".join(f"{n}={g}" for n, g in governors[:4])
    if not check(all_powersave, "CPU governor = powersave (all cores)", detail):
        fails += 1

    # 4. ACT LED trigger should be 'none' (brackets mark the selected trigger).
    led_ok = False
    led_detail = "no LED node found"
    for path in ("/sys/class/leds/ACT/trigger", "/sys/class/leds/led0/trigger"):
        if os.path.exists(path):
            t = cat(path)
            led_detail = t[:60]
            led_ok = "[none]" in t
            break
    if not check(led_ok, "ACT LED trigger = none", led_detail):
        fails += 1

    # 5. power.py importable and functional.
    sys.path.insert(0, "/home/pi/kidpager")
    try:
        import power
        en = power.wifi_is_enabled()
        check(True, "power.py importable", f"wifi_is_enabled() -> {en}")
    except Exception as e:
        check(False, "power.py importable", str(e)[:60])
        fails += 1

    # 6. kidpager-power.sh script present and executable.
    script = "/usr/local/bin/kidpager-power.sh"
    script_ok = os.path.isfile(script) and os.access(script, os.X_OK)
    if not check(script_ok, f"{script} executable"):
        fails += 1

    print()
    if fails:
        print(f"FAIL: {fails} issue(s)")
        sys.exit(1)
    print("All power checks passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
