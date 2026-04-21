# KidPager Deploy
#
# Usage:
#   .\deploy.ps1 -Setup                             # once per machine: push SSH key to both pagers
#   .\deploy.ps1 -All                               # deploy to both pagers
#   .\deploy.ps1 -PiHost kidpager.local             # deploy to one pager
#   .\deploy.ps1 -Restart                           # restart service on both
#   .\deploy.ps1 -WipeHistory                       # clear chat history on both
#   .\deploy.ps1 -All -WipeHistory                  # deploy then wipe
#   .\deploy.ps1 -All -Tests                        # deploy + also copy test_*.py
#   .\deploy.ps1 -Diag                              # run full diagnose.py on both
#   .\deploy.ps1 -Diag -PiHost kidpager.local       # diagnose one
#
# Target OS: Raspberry Pi OS Trixie Lite (Python 3.13) on Pi Zero 2 W.
#
# Idempotent: safe to re-run. Repairs a broken install (missing pigpiod daemon,
# missing systemd unit, non-executable bt_pair.sh, stale config) without
# re-flashing.
#
# ---------------------------------------------------------------------------
# Pre-requisites (set in rpi-imager BEFORE flashing -- saves half a day of
# post-boot fiddling):
#
#   * Hostname            kidpager   (or kidpager2 for the second device)
#   * SSH                 enabled
#   * Wi-Fi               SSID + password (no ethernet on Pi Zero 2 W)
#   * Username            pi
#   * Passwordless sudo   enabled  <-- CRITICAL. Deploy uses 'sudo foo' without
#                                      -S everywhere; each missing NOPASSWD
#                                      entry = hangs waiting on stdin.
#
# Forgot passwordless sudo? Fix before running this script:
#   ssh -t pi@kidpager.local
#   echo 'pi ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/010_pi-nopasswd
#   sudo chmod 0440 /etc/sudoers.d/010_pi-nopasswd
#   sudo visudo -c
# ---------------------------------------------------------------------------

param(
    [string]$PiHost = "",
    [string]$PiUser = "pi",
    [switch]$All,
    [switch]$Setup,
    [switch]$Restart,
    [switch]$WipeHistory,
    [switch]$Tests,
    [switch]$Diag
)

$PAGERS = @("kidpager.local", "kidpager2.local")
$KEY = "$env:USERPROFILE\.ssh\id_kidpager"
# UserKnownHostsFile=/dev/null + LogLevel=ERROR: survives SD re-flashes without
# manual ssh-keygen -R. We're on a LAN authenticating by key; MITM risk is the
# same as the plain StrictHostKeyChecking=no we were using before anyway.
$sshCmd = @(
    "-F", "nul",
    "-i", $KEY,
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR"
)

# Windows mDNS (Bonjour) is flaky on long-running deploys: the cache can
# expire or the resolver can stutter mid-deploy, killing steps 6/7/8 with
# "Could not resolve hostname". Resolve once up front, cache the IP, reuse it
# for every subsequent SSH invocation against this pager. If the pager moves
# we just re-run deploy.
#
# Uses [IPAddress]::TryParse to pass through already-numeric hosts and
# [System.Net.Dns]::GetHostAddresses for lookups (which honors the Windows
# mDNS resolver on Win10+). Retries 5x with 500ms gaps to ride out transient
# Bonjour hiccups.
function Resolve-Target {
    param([string]$HostName)
    $tmp = $null
    if ([System.Net.IPAddress]::TryParse($HostName, [ref]$tmp)) { return $HostName }
    for ($i = 0; $i -lt 5; $i++) {
        try {
            $addrs = [System.Net.Dns]::GetHostAddresses($HostName)
            $ipv4  = $addrs | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1
            if ($ipv4) { return $ipv4.IPAddressToString }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    return $null
}

# Production code + diagnose.py (useful in the field).
$PY_FILES = @("pins.py","lora.py","display_eink.py","config.py","keyboard.py","buzzer.py","ui.py","main.py","power.py","diagnose.py")
# Developer-only smoke tests; only copied when -Tests is passed.
$TEST_FILES = @("test_lora_spi.py","test_buzzer.py","test_power.py","test_retry.py")

# Service runs as User=root, so ~ expands to /root -- live history lives there.
# /home/pi path is cleared too in case stale files remain from older deploys.
$WIPE_CMD = "sudo systemctl stop kidpager 2>/dev/null; sudo rm -f /root/.kidpager/history.json /home/pi/.kidpager/history.json; sudo systemctl start kidpager 2>/dev/null; echo wiped"

if ($Setup) {
    if (!(Test-Path $KEY)) { ssh-keygen -t ed25519 -N '""' -f $KEY }
    foreach ($dest in $PAGERS) {
        $ip = Resolve-Target $dest
        if (-not $ip) { Write-Host "$dest UNREACHABLE (DNS)" -ForegroundColor Red; continue }
        Write-Host "Key -> $dest ($ip) (password once)..." -ForegroundColor Cyan
        Get-Content "${KEY}.pub" | ssh -F nul -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "${PiUser}@${ip}" "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys" 2>$null
    }
    Write-Host "Done! Run: .\deploy.ps1 -All" -ForegroundColor Green; exit 0
}

if ($Restart) {
    foreach ($dest in $PAGERS) {
        $ip = Resolve-Target $dest
        if (-not $ip) { Write-Host "$dest UNREACHABLE (DNS)" -ForegroundColor Red; continue }
        ssh @sshCmd "${PiUser}@${ip}" "sudo systemctl restart kidpager 2>/dev/null && echo $dest OK || echo $dest FAIL" 2>$null
    }
    exit 0
}

# Remote health check: runs diagnose.py on-device (-y auto-stops kidpager for
# HW tests). Use -PiHost to target a single pager, otherwise runs on both.
if ($Diag) {
    $targets = if ($PiHost) { @($PiHost) } else { $PAGERS }
    foreach ($dest in $targets) {
        $ip = Resolve-Target $dest
        if (-not $ip) { Write-Host "$dest UNREACHABLE (DNS)" -ForegroundColor Red; continue }
        Write-Host "`n=== Diag $dest ($ip) ===" -ForegroundColor Yellow
        ssh @sshCmd "${PiUser}@${ip}" "cd /home/pi/kidpager && sudo python3 diagnose.py -y" 2>$null
    }
    exit 0
}

# Standalone wipe (no deploy)
if ($WipeHistory -and -not $All -and -not $PiHost) {
    foreach ($dest in $PAGERS) {
        $ip = Resolve-Target $dest
        if (-not $ip) { Write-Host "$dest UNREACHABLE (DNS)" -ForegroundColor Red; continue }
        Write-Host "Wipe history -> $dest" -ForegroundColor Magenta
        ssh @sshCmd "${PiUser}@${ip}" $WIPE_CMD 2>$null
    }
    exit 0
}

if ($All) { $targets = $PAGERS }
elseif ($PiHost) { $targets = @($PiHost) }
else { Write-Host "Usage: -Setup | -All | -PiHost name | -Restart | -WipeHistory | -Diag [-PiHost name]"; exit 1 }

if (!(Test-Path $KEY)) { Write-Host "Run -Setup first" -ForegroundColor Red; exit 1 }

foreach ($dest in $targets) {
    Write-Host "`n=== $dest ===" -ForegroundColor Yellow

    # Resolve hostname -> IP once. Use the IP for all 8 deploy steps so a
    # mid-deploy Bonjour hiccup can't drop steps 6/7/8 with a DNS failure.
    $ip = Resolve-Target $dest
    if (-not $ip) { Write-Host "  UNREACHABLE (DNS could not resolve $dest)" -ForegroundColor Red; continue }
    if ($ip -ne $dest) { Write-Host "  $dest -> $ip" -ForegroundColor DarkGray }
    $t = "${PiUser}@${ip}"

    $ok = ssh @sshCmd $t "echo ok" 2>$null
    if ($ok -ne "ok") { Write-Host "  UNREACHABLE (SSH)" -ForegroundColor Red; continue }

    Write-Host "  [1/8] Packages" -ForegroundColor Cyan
    # python3-pigpio is the Python client library, available on Trixie.
    # The pigpiod daemon itself is NOT packaged on Trixie Lite -- git +
    # build-essential are needed to build it from source in step [2/8].
    ssh @sshCmd $t "sudo apt update -qq 2>/dev/null; sudo apt install -y python3-spidev python3-rpi.gpio python3-pil python3-gpiozero python3-pigpio git build-essential bluez fonts-dejavu-core wget rfkill 2>/dev/null | tail -1"

    Write-Host "  [2/8] SPI + pigpiod (build daemon from source)" -ForegroundColor Cyan
    # Trixie Lite is supposed to have python3-pigpio (client library) and we
    # build pigpiod (C daemon) from source. In practice python3-pigpio
    # sometimes silently fails to install (network hiccup, apt errors hidden
    # by 2>/dev/null in [1/8]), and then `import pigpio` fails even though
    # the daemon is running. Fix: if the Python module is missing, we copy
    # pigpio.py straight out of the cloned source tree (it's pure Python, no
    # compile needed) into the system site-packages. Self-heals whatever
    # combination of pieces is broken.
    #
    # Idempotent:
    #   1. pigpiod binary  -- skip build if /usr/local/bin/pigpiod exists
    #   2. pigpio.py       -- fix only if `import pigpio` fails
    #   3. systemd unit    -- write only if missing
    #   4. start           -- enable --now every run (cheap if already active)
    ssh @sshCmd $t @"
set -e
sudo raspi-config nonint do_spi 0 2>/dev/null || true

# --- (1) pigpiod C daemon -------------------------------------------------
if [ ! -x /usr/local/bin/pigpiod ]; then
    echo '  Building pigpio from source (2-3 minutes, be patient)...'
    cd /tmp && rm -rf pigpio
    git clone --depth 1 https://github.com/joan2937/pigpio.git >/dev/null 2>&1
    cd pigpio && make -j4 >/dev/null 2>&1
    # We only need the C library + daemon binary. The Makefile's Python-module
    # install step fails on Py3.12+ (distutils removed); we handle Python
    # bindings separately below.
    sudo make install 2>/dev/null || true
    sudo ldconfig
    echo '  pigpiod built and installed'
else
    echo '  pigpiod daemon already installed'
fi

# --- (2) pigpio Python client --------------------------------------------
# Try apt path first, then fall back to copying pigpio.py from source.
# `python3 -c 'import pigpio'` is the source of truth: if it succeeds we
# don't care where the file came from.
if ! python3 -c 'import pigpio' 2>/dev/null; then
    echo '  pigpio Python module missing, self-healing...'
    # Fallback: snag pigpio.py straight out of the source tree. It's a pure
    # Python socket client; no compile, no setup.py needed.
    if [ ! -f /tmp/pigpio/pigpio.py ]; then
        cd /tmp && rm -rf pigpio
        git clone --depth 1 https://github.com/joan2937/pigpio.git >/dev/null 2>&1
    fi
    DEST=`$(python3 -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null)
    [ -z "`$DEST" ] && DEST=/usr/local/lib/python3/dist-packages
    sudo mkdir -p "`$DEST"
    sudo cp /tmp/pigpio/pigpio.py "`$DEST/pigpio.py"
    echo "  pigpio.py copied to `$DEST"
fi

# --- (3) systemd unit ----------------------------------------------------
if [ ! -f /lib/systemd/system/pigpiod.service ]; then
    sudo tee /lib/systemd/system/pigpiod.service >/dev/null <<'UNIT'
[Unit]
Description=Daemon required to control GPIO pins via pigpio

[Service]
ExecStart=/usr/local/bin/pigpiod -l
ExecStop=/bin/systemctl kill pigpiod
Type=forking

[Install]
WantedBy=multi-user.target
UNIT
    echo '  pigpiod.service unit installed'
fi

# --- (4) start + verify --------------------------------------------------
sudo systemctl daemon-reload
sudo systemctl enable pigpiod --now 2>/dev/null
sleep 1
systemctl is-active pigpiod 2>/dev/null | grep -q active && echo '  pigpiod: active' || echo '  pigpiod: FAILED'
# Verify the client can reach the socket. Catches a race where the daemon
# is "active" but not yet listening, plus catches a missing binding.
python3 -c 'import pigpio; p=pigpio.pi(); print("  pigpio socket: OK" if p.connected else "  pigpio socket: UNREACHABLE"); (p.stop() if p.connected else None)' 2>/dev/null || echo '  pigpio module: IMPORT FAILED'
"@

    Write-Host "  [3/8] Waveshare E-Ink driver" -ForegroundColor Cyan
    ssh @sshCmd $t "mkdir -p ~/waveshare_epd; B=https://raw.githubusercontent.com/waveshare/e-Paper/master/RaspberryPi_JetsonNano/python/lib/waveshare_epd; for F in __init__.py epdconfig.py epd2in13_V4.py; do test -f ~/waveshare_epd/`$F || wget -q -O ~/waveshare_epd/`$F `$B/`$F; done; test -f ~/waveshare_epd/epd2in13_V4.py && echo OK || echo FAIL"

    Write-Host "  [4/8] Files" -ForegroundColor Cyan
    ssh @sshCmd $t "mkdir -p ~/kidpager ~/.kidpager"
    $filesToSend = $PY_FILES
    if ($Tests) { $filesToSend = $PY_FILES + $TEST_FILES }
    foreach ($f in $filesToSend) {
        if (Test-Path $f) {
            $bytes = [System.IO.File]::ReadAllBytes($f)
            $clean = $bytes | Where-Object { $_ -ne 0 }
            $tmp = [System.IO.Path]::GetTempFileName()
            [System.IO.File]::WriteAllBytes($tmp, [byte[]]$clean)
            scp @sshCmd -q $tmp "${t}:~/kidpager/${f}" 2>$null
            Remove-Item $tmp
        }
    }
    # bt_pair.sh stays in ~/. kidpager-power.sh gets installed to /usr/local/bin/
    # because its systemd unit ExecStart= references that path.
    foreach ($sh in @("bt_pair.sh", "kidpager-power.sh")) {
        if (Test-Path $sh) {
            $clean = (Get-Content $sh -Raw) -replace "`r", ""
            [System.IO.File]::WriteAllText("$env:TEMP\$sh", $clean, [System.Text.UTF8Encoding]::new($false))
            scp @sshCmd -q "$env:TEMP\$sh" "${t}:~/${sh}" 2>$null
            Remove-Item "$env:TEMP\$sh"
        }
    }
    # scp preserves source permissions but Windows filesystem has no +x bit, so
    # shell scripts land on the Pi as 0644. Make bt_pair.sh executable so
    # `sudo ~/bt_pair.sh` works without the user having to chmod first.
    # kidpager-power.sh gets its exec bit from `install -m 755` below.
    ssh @sshCmd $t "chmod +x ~/bt_pair.sh 2>/dev/null; sudo install -m 755 ~/kidpager-power.sh /usr/local/bin/kidpager-power.sh && rm ~/kidpager-power.sh"

    Write-Host "  [5/8] Config" -ForegroundColor Cyan
    # Remove stale /home/pi/.kidpager/config.json from pre-v0.9 deploys; live
    # config lives in /root/.kidpager/ because the service runs as root.
    # Existing /root/.kidpager/config.json is NEVER overwritten (guarded by
    # `test -f`) -- preserves the user's name, channel, and silent flag
    # across redeploys.
    ssh @sshCmd $t "sudo rm -f /home/pi/.kidpager/config.json; sudo mkdir -p /root/.kidpager; sudo test -f /root/.kidpager/config.json || echo '{""name"":""Kid"",""channel"":1,""silent"":false}' | sudo tee /root/.kidpager/config.json >/dev/null"

    Write-Host "  [6/8] Service" -ForegroundColor Cyan
    ssh @sshCmd $t "sudo bash -c 'cat > /etc/systemd/system/kidpager.service << SVCEOF
[Unit]
Description=KidPager
After=multi-user.target bluetooth.target pigpiod.service kidpager-power.service
Wants=bluetooth.target pigpiod.service kidpager-power.service
[Service]
Type=simple
User=root
WorkingDirectory=/home/pi/kidpager
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/usr/sbin/rfkill unblock bluetooth
ExecStart=/usr/bin/python3 -u /home/pi/kidpager/main.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload && systemctl enable kidpager && echo OK'"

    Write-Host "  [7/8] Power-save service" -ForegroundColor Cyan
    # Runs /usr/local/bin/kidpager-power.sh once at boot: rfkill wifi,
    # powersave governor, ACT LED off. Before=kidpager.service so the main
    # pager starts in the already-saved state.
    #
    # IMPORTANT: enable WITHOUT --now. The oneshot does 'rfkill block wifi',
    # which would sever the SSH connection we're deploying over. Power-save
    # activates on next reboot -- harmless because the Pi reboots at the end
    # of field setup anyway (and Alt+W on the M4 re-enables Wi-Fi for
    # re-deploys later).
    ssh @sshCmd $t "sudo bash -c 'cat > /etc/systemd/system/kidpager-power.service << PWREOF
[Unit]
Description=KidPager power-saving (rfkill wifi, powersave governor, LED off)
After=multi-user.target
Before=kidpager.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/kidpager-power.sh
[Install]
WantedBy=multi-user.target
PWREOF
systemctl daemon-reload && systemctl enable kidpager-power && echo OK'"

    Write-Host "  [8/8] Verify" -ForegroundColor Cyan
    # Wi-Fi-blocked and CPU-governor checks are intentionally omitted here --
    # they would FAIL until first reboot (see step 7/8 comment). Full health
    # check post-reboot: .\deploy.ps1 -Diag
    ssh @sshCmd $t @"
echo '---'
test -f ~/waveshare_epd/epd2in13_V4.py && echo '[OK] Waveshare driver'       || echo '[!!] Waveshare driver'
test -f ~/kidpager/main.py              && echo '[OK] Code deployed'          || echo '[!!] Code missing'
test -x ~/bt_pair.sh                    && echo '[OK] bt_pair.sh executable'  || echo '[!!] bt_pair.sh NOT executable'
test -x /usr/local/bin/kidpager-power.sh && echo '[OK] Power script'          || echo '[!!] Power script missing'
test -x /usr/local/bin/pigpiod          && echo '[OK] pigpiod binary'         || echo '[!!] pigpiod binary missing'
test -f /lib/systemd/system/pigpiod.service && echo '[OK] pigpiod unit'       || echo '[!!] pigpiod unit missing'
ls /dev/spidev0.0 >/dev/null 2>&1       && echo '[OK] SPI CE0 (E-Ink)'        || echo '[!!] SPI CE0 missing'
ls /dev/spidev0.1 >/dev/null 2>&1       && echo '[OK] SPI CE1 (LoRa)'         || echo '[!!] SPI CE1 missing'
test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf && echo '[OK] Fonts'  || echo '[!!] Fonts missing'
systemctl is-enabled kidpager       2>/dev/null | grep -q enabled && echo '[OK] kidpager autostart'          || echo '[!!] kidpager autostart'
systemctl is-enabled kidpager-power 2>/dev/null | grep -q enabled && echo '[OK] Power-save (active on boot)' || echo '[!!] Power-save NOT enabled'
systemctl is-active  pigpiod        2>/dev/null | grep -q active  && echo '[OK] pigpiod running'             || echo '[!!] pigpiod NOT running'
python3 -c 'import pigpio' 2>/dev/null && echo '[OK] pigpio Python module'    || echo '[!!] pigpio module missing'
echo 'BT paired devices:'
bluetoothctl devices 2>/dev/null | sed 's/^/  /' || echo '  (none)'
echo '---'
"@

    if ($WipeHistory) {
        Write-Host "  [+]   Wipe history" -ForegroundColor Magenta
        ssh @sshCmd $t $WIPE_CMD
    }

    Write-Host "  $dest DONE" -ForegroundColor Green
}

Write-Host "`n=== Complete ===" -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Pair the M4 on each pager: ssh pi@kidpager.local -> sudo ~/bt_pair.sh" -ForegroundColor Yellow
Write-Host "  2. Reboot each pager once (power-save activates on boot)." -ForegroundColor Yellow
Write-Host "  3. Verify: .\deploy.ps1 -Diag" -ForegroundColor Yellow
Write-Host "  (After reboot Wi-Fi is blocked. Alt+W on the M4 re-enables it for re-deploys.)" -ForegroundColor Yellow
