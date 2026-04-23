#!/bin/bash
# Offline repair for a KidPager stuck with Wi-Fi unreachable.
#
# Symptom: W badge lights up on the E-Ink, but the pager never appears on
# the LAN (no ARP entry, no DHCP lease). Cause: the v0.13 Alt+W toggle
# only flipped rfkill; NetworkManager's internal radio state stayed
# `WirelessEnabled=false` and persists across reboots, so rfkill-unblock
# alone never translates into an actual connection. Fixed in v0.14+ by
# syncing rfkill and nmcli in lockstep, but the v0.13 pager needs an
# offline nudge because we can't SSH in to run nmcli.
#
# What this script does, with the pager's microSD mounted on a Linux or
# WSL2 machine:
#
#   1. Rewrites /var/lib/NetworkManager/NetworkManager.state so NM starts
#      with its radio enabled on next boot.
#   2. Removes the kidpager-power.service autostart symlink so the old
#      rfkill-block-at-boot doesn't immediately undo step 1. The next
#      `deploy.ps1` run will re-enable it (and the updated power script
#      has proper NM sync, so the v0.13 bug can't come back).
#
# Pass the rootfs mount point as the only argument. Not /boot — the ext4
# partition.
#
# Usage (native Linux / Pi):
#   sudo ./fix-wifi-offline.sh /mnt/pager-root
#
# Usage (Windows + WSL2, SD in a USB reader):
#   1. In an admin PowerShell, find the physical drive:
#        wmic diskdrive list brief
#      note the DeviceID for your SD reader (e.g. \\.\PHYSICALDRIVE2)
#   2. Attach it to WSL:
#        wsl --mount \\.\PHYSICALDRIVE2 --partition 2
#      ("2" is the rootfs partition; partition 1 is /boot FAT32)
#   3. In WSL shell:
#        sudo ./fix-wifi-offline.sh /mnt/wsl/PHYSICALDRIVE2p2
#   4. When done:
#        wsl --unmount \\.\PHYSICALDRIVE2

set -euo pipefail

ROOT="${1:-}"

if [ -z "$ROOT" ] || [ ! -d "$ROOT" ]; then
    echo "Usage: $0 <rootfs-mountpoint>" >&2
    echo "  example: sudo $0 /mnt/wsl/PHYSICALDRIVE2p2" >&2
    exit 1
fi

# Sanity check the mount point. The rootfs contains /var/lib (among
# many other things); the FAT /boot partition does not. Mistaking one
# for the other is the most common footgun, especially under WSL where
# `wsl --mount` without `--partition` attaches the whole disk and the
# default path looks the same.
if [ ! -d "$ROOT/var/lib" ] || [ ! -d "$ROOT/etc/systemd" ]; then
    echo "ERROR: $ROOT doesn't look like a Pi rootfs." >&2
    echo "  Expected /var/lib and /etc/systemd to exist." >&2
    echo "  If you mounted the FAT boot partition by mistake, try" >&2
    echo "  the other partition (partition 2 on a standard rpi-imager" >&2
    echo "  layout is the ext4 rootfs)." >&2
    exit 1
fi

NM_STATE="$ROOT/var/lib/NetworkManager/NetworkManager.state"
PWR_LINK="$ROOT/etc/systemd/system/multi-user.target.wants/kidpager-power.service"

echo "=== Before ==="
echo "-- $NM_STATE --"
if [ -f "$NM_STATE" ]; then
    sed 's/^/  /' "$NM_STATE"
else
    echo "  (file missing — NM may have never run on this SD)"
fi
echo "-- kidpager-power.service autostart --"
if [ -L "$PWR_LINK" ]; then
    echo "  ENABLED ($(readlink "$PWR_LINK"))"
else
    echo "  not enabled"
fi
echo

# --- Fix 1: NetworkManager.state ---------------------------------------------
# NM's state file is INI-ish. Lines look like:
#   [main]
#   NetworkingEnabled=true
#   WirelessEnabled=false
#   WWANEnabled=true
# We set WirelessEnabled=true. If the line exists, swap it in place; if
# it's missing we append under [main]. If the whole file is missing
# (fresh rootfs), write a full good copy — NM will accept it.

mkdir -p "$(dirname "$NM_STATE")"

if [ -f "$NM_STATE" ]; then
    if grep -q '^WirelessEnabled=' "$NM_STATE"; then
        sed -i 's/^WirelessEnabled=.*/WirelessEnabled=true/' "$NM_STATE"
    else
        # No line to rewrite — append under [main]. awk is clearer than sed
        # for "insert after section header" and handles the case where
        # [main] isn't first.
        awk '
            BEGIN { inserted = 0 }
            /^\[main\]/ && !inserted { print; print "WirelessEnabled=true"; inserted = 1; next }
            { print }
            END { if (!inserted) { print "[main]"; print "WirelessEnabled=true" } }
        ' "$NM_STATE" > "$NM_STATE.new"
        mv "$NM_STATE.new" "$NM_STATE"
    fi
else
    cat > "$NM_STATE" <<'EOF'
[main]
NetworkingEnabled=true
WirelessEnabled=true
WWANEnabled=true
EOF
fi

# --- Fix 2: disable kidpager-power.service autostart -------------------------
# The v0.13 script does `rfkill block wifi` unconditionally at boot, which
# would wipe the NM fix we just wrote within seconds. Remove the symlink
# only — leave the service unit file in place so `systemctl enable
# kidpager-power` during the upcoming deploy restores it cleanly.
if [ -L "$PWR_LINK" ]; then
    rm -f "$PWR_LINK"
fi

# Belt-and-braces: force fs buffers to disk before the user pulls the SD.
sync

echo "=== After ==="
echo "-- $NM_STATE --"
sed 's/^/  /' "$NM_STATE"
echo "-- kidpager-power.service autostart --"
if [ -L "$PWR_LINK" ]; then
    echo "  ENABLED (??)"
else
    echo "  DISABLED — will be re-enabled by next deploy.ps1 run"
fi
echo
echo "Done. Unmount the SD, put it back in the pager, boot."
echo "Expected: Wi-Fi comes up on its own (no Alt+W needed), the pager"
echo "appears in ARP within ~30 s. Then run from Windows:"
echo "  .\\deploy.ps1 <host>.local"
