"""Power management helpers for KidPager.

Wi-Fi is soft-blocked at boot by kidpager-power.service (powersave mode).
The pager user can re-enable it on demand with Alt+W (debug / deploy over SSH),
and a `W` badge appears on the E-Ink header while it's on.

We deliberately don't touch Bluetooth: the M4 keyboard needs it permanently
up, and BT Classic HID is already cheap (~10 mA).

Why the toggle touches both rfkill AND NetworkManager
-----------------------------------------------------
On Trixie, NetworkManager maintains its OWN `radio wifi on/off` state,
separate from the kernel rfkill soft-block. When `kidpager-power.service`
does `rfkill block wifi` at boot, NM observes the blocked state and sets
its internal radio to off. Later clearing rfkill alone does NOT tell NM
to re-enable its radio — Alt+W would flip the `W` badge but the pager
would silently stay offline. Explicit `nmcli radio wifi on` +
`nmcli connection up` after rfkill unblock forces NM to attempt the
saved connection. Same dance in reverse on block so NM doesn't keep
retrying the SSID while rfkill is blocking the radio.
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
    """Flip Wi-Fi rfkill state AND sync NetworkManager radio state so the
    saved connection actually comes up (or goes down). Returns the actual
    state after the attempt (True = ON) — the UI shows reality, not intent,
    so a failed toggle doesn't light up the W badge falsely."""
    currently_blocked = _rfkill_wifi_blocked()
    try:
        if currently_blocked:
            # Going ON: unblock rfkill first, then kick NM into wanting
            # Wi-Fi, then nudge the device to attempt any auto-connect
            # profile. Each step is best-effort; we swallow exceptions so
            # a missing nmcli (edge case) doesn't wedge the toggle.
            subprocess.run(["rfkill", "unblock", "wifi"],
                           capture_output=True, timeout=3, check=True)
            subprocess.run(["nmcli", "radio", "wifi", "on"],
                           capture_output=True, timeout=3)
            # `nmcli device connect wlan0` forces NM to pick up the
            # autoconnect profile even if it gave up at boot.
            subprocess.run(["nmcli", "device", "connect", "wlan0"],
                           capture_output=True, timeout=5)
        else:
            # Going OFF: flip NM off first so it stops trying to reassociate
            # while we tear down the radio via rfkill.
            subprocess.run(["nmcli", "radio", "wifi", "off"],
                           capture_output=True, timeout=3)
            subprocess.run(["rfkill", "block", "wifi"],
                           capture_output=True, timeout=3, check=True)
    except Exception as e:
        print(f"wifi_toggle error: {e}")
    return not _rfkill_wifi_blocked()
