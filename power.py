"""Power management helpers for KidPager.

Wi-Fi is soft-blocked at boot by kidpager-power.service (powersave mode).
The pager user can re-enable it on demand with Alt+W (debug / deploy over SSH),
and a `W` badge appears on the E-Ink header while it's on.

We deliberately don't touch Bluetooth: the M4 keyboard needs it permanently
up, and BT Classic HID is already cheap (~10 mA).
"""
import subprocess


def _rfkill_wifi_blocked():
    """True if Wi-Fi is soft-blocked (radio off). Treat ambiguous states
    (no wifi device, rfkill missing, exception) as blocked — "assume off"
    is the safer default for the UI indicator so the W badge never shows
    when radio is not actually available."""
    try:
        r = subprocess.run(
            ["rfkill", "list", "wifi"],
            capture_output=True, text=True, timeout=2
        )
        out = r.stdout
        if "Soft blocked: yes" in out:
            return True
        if "Soft blocked: no" in out:
            return False
        # Empty output = no wifi device on this Pi, or rfkill returned nothing.
        return True
    except Exception:
        return True


def wifi_is_enabled():
    return not _rfkill_wifi_blocked()


def wifi_toggle():
    """Flip Wi-Fi rfkill state. Returns the actual state after the attempt
    (True = ON) — the UI shows reality, not intent, so a failed toggle
    doesn't light up the W badge falsely."""
    currently_blocked = _rfkill_wifi_blocked()
    try:
        cmd = ["rfkill", "unblock" if currently_blocked else "block", "wifi"]
        subprocess.run(cmd, capture_output=True, timeout=3, check=True)
    except Exception as e:
        print(f"wifi_toggle error: {e}")
    return not _rfkill_wifi_blocked()
