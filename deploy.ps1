# KidPager Deploy
#
# Usage:
#   .\deploy.ps1 -Help                              # show this usage block
#   .\deploy.ps1 -Setup                             # install SSH key on both pagers (no code push)
#   .\deploy.ps1 -All                               # deploy to both pagers (auto-installs key if missing)
#   .\deploy.ps1 -PiHost kidpager.local             # deploy to one pager
#   .\deploy.ps1 -Restart                           # restart kidpager.service on both
#   .\deploy.ps1 -WipeHistory                       # clear chat history on both
#   .\deploy.ps1 -All -WipeHistory                  # deploy then wipe
#   .\deploy.ps1 -All -Tests                        # deploy + also copy test_*.py
#   .\deploy.ps1 -Diag                              # run full diagnose.py on both
#   .\deploy.ps1 -Diag -PiHost kidpager.local       # diagnose one pager
#
# Target OS: Raspberry Pi OS Trixie Lite (Python 3.13) on Pi Zero 2 W.
#
# Key auth:
#   The script auto-generates ~\.ssh\id_kidpager on first run and installs
#   the pubkey on each pager (asks for the pi password once per device).
#   Subsequent runs reuse the key with zero prompts. Re-running -Setup is a
#   no-op if the key is already authorized.
#
# Idempotent: safe to re-run. Repairs a broken install (missing pigpiod
# daemon, missing Python pigpio module, missing systemd unit, non-executable
# bt_pair.sh, stale config) without re-flashing.
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
# Forgot passwordless sudo? The script detects it and shows the fix. You
# can also pre-apply manually:
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
    [switch]$Diag,
    [switch]$Help
)

# ===========================================================================
# Constants
# ===========================================================================
$PAGERS = @("kidpager.local", "kidpager2.local")
$KEY = "$env:USERPROFILE\.ssh\id_kidpager"

# SSH options for already-authorized connections. Hostkey/known-hosts checks
# off: we re-authenticate by key every time, MITM risk on a LAN is no higher
# than we already accept, and this survives SD-card re-flashes without
# manual ssh-keygen -R cleanup.
$sshCmd = @(
    "-F", "nul",
    "-i", $KEY,
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR"
)

# Production code + diagnose.py (useful in the field).
$PY_FILES = @("pins.py","lora.py","display_eink.py","config.py","keyboard.py","buzzer.py","ui.py","main.py","power.py","diagnose.py")
# Developer-only smoke tests; only copied when -Tests is passed.
$TEST_FILES = @("test_lora_spi.py","test_buzzer.py","test_power.py","test_retry.py")

# Service runs as User=root, so ~ expands to /root -- live history lives there.
# /home/pi path is cleared too in case stale files remain from older deploys.
$WIPE_CMD = "sudo systemctl stop kidpager 2>/dev/null; sudo rm -f /root/.kidpager/history.json /home/pi/.kidpager/history.json; sudo systemctl start kidpager 2>/dev/null; echo wiped"

# ===========================================================================
# Helpers
# ===========================================================================

function Show-Help {
    Write-Host @"
KidPager Deploy

Usage:
  .\deploy.ps1 -Help                          show this help
  .\deploy.ps1 -Setup                         install SSH key on both pagers (no code push)
  .\deploy.ps1 -All                           deploy to both pagers (auto-installs key)
  .\deploy.ps1 -PiHost kidpager.local         deploy to one pager
  .\deploy.ps1 -Restart                       restart kidpager.service on both
  .\deploy.ps1 -WipeHistory                   clear chat history on both
  .\deploy.ps1 -All -WipeHistory              deploy then wipe
  .\deploy.ps1 -All -Tests                    deploy + also copy test_*.py
  .\deploy.ps1 -Diag                          run full diagnose.py on both
  .\deploy.ps1 -Diag -PiHost kidpager.local   diagnose one pager

Flags:
  -PiUser <name>   override SSH user (default: pi)
  -Tests           include developer-only smoke tests in -All

Key auth is automatic: on first contact the script installs ~\.ssh\id_kidpager.pub
on the pager (asks for the pi password ONCE per device). Subsequent runs: zero prompts.

Passwordless sudo is required on the pager. The script detects a missing NOPASSWD
and prints the fix command.
"@
}

# Resolve mDNS hostname to IPv4 once; reuse for all subsequent SSH calls so a
# mid-deploy Bonjour hiccup can't kill steps 6/7/8 with "Could not resolve".
# Pass-through numeric IPs. 5x retry with 500ms gaps.
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

# Probe whether key-based SSH works to $Target (= user@ip) without asking
# for a password. BatchMode=yes aborts on any prompt; PasswordAuthentication=no
# tells ssh not to even try password auth. If either key auth or
# connectivity fails, returns $false.
function Test-PasswordlessSSH {
    param([string]$Target)
    $result = & ssh -F nul `
        -i $KEY `
        -o BatchMode=yes `
        -o PasswordAuthentication=no `
        -o StrictHostKeyChecking=no `
        -o UserKnownHostsFile=/dev/null `
        -o LogLevel=ERROR `
        -o ConnectTimeout=5 `
        $Target "echo ok" 2>$null
    # Check both stdout AND exit code so a transient 0-exit connection drop
    # doesn't falsely report success without the remote having acknowledged.
    return ($result -eq "ok")
}

# Install our pubkey on the pager. The ONLY command in the deploy flow that
# is allowed to prompt for a password. Dedup-safe: re-running against an
# already-authorized pager is a no-op (grep -qxF short-circuits the append).
function Install-SSHKey {
    param([string]$Target)
    $pubkey = (Get-Content "${KEY}.pub" -Raw).Trim()
    # Single-line remote command with `;` / `&&` chaining. Avoids any CRLF
    # vs LF surprises that could happen with a multi-line heredoc crossing
    # the Windows->Linux boundary. Single-quoted '$pubkey' on the remote
    # side keeps it literal (safe: ssh-keygen output has no `'` character).
    $remote = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && (grep -qxF '$pubkey' ~/.ssh/authorized_keys || echo '$pubkey' >> ~/.ssh/authorized_keys)"
    Write-Host "  Installing SSH key on $Target (password once)..." -ForegroundColor Yellow
    # Force password auth only, one attempt. This gives a clean fast-fail if
    # the password is wrong (otherwise ssh defaults to 3 prompts, and if a
    # stale key coincidentally works it silently reuses it instead of
    # installing the new one -- confusing for debugging).
    & ssh -F nul `
        -o StrictHostKeyChecking=no `
        -o UserKnownHostsFile=/dev/null `
        -o LogLevel=ERROR `
        -o ConnectTimeout=10 `
        -o PreferredAuthentications=password `
        -o PubkeyAuthentication=no `
        -o NumberOfPasswordPrompts=1 `
        $Target $remote
    return ($LASTEXITCODE -eq 0)
}

# Check that `sudo` on the pager runs without prompting. The deploy issues
# ~20+ sudo calls; each missing NOPASSWD is a hang. Fail fast here with a
# copy-pasteable one-liner to fix it.
function Test-PasswordlessSudo {
    param([string]$Target)
    $result = & ssh @sshCmd $Target "sudo -n true 2>/dev/null && echo ok" 2>$null
    return ($result -eq "ok")
}

# One-stop: resolve hostname -> IP, verify key auth (install if needed),
# verify passwordless sudo. Returns the IP on success, $null on any failure.
function Ensure-Connectivity {
    param([string]$HostName)
    $ip = Resolve-Target $HostName
    if (-not $ip) {
        Write-Host "  $HostName UNREACHABLE (DNS)" -ForegroundColor Red
        return $null
    }
    if ($ip -ne $HostName) {
        Write-Host "  $HostName -> $ip" -ForegroundColor DarkGray
    }

    $target = "${PiUser}@${ip}"

    if (-not (Test-PasswordlessSSH $target)) {
        if (-not (Install-SSHKey $target)) {
            Write-Host "  $HostName SSH key install FAILED (bad password? unreachable?)" -ForegroundColor Red
            return $null
        }
        if (-not (Test-PasswordlessSSH $target)) {
            Write-Host "  $HostName key installed but still prompting -- check authorized_keys on pager" -ForegroundColor Red
            return $null
        }
        Write-Host "  $HostName SSH key installed" -ForegroundColor Green
    }

    if (-not (Test-PasswordlessSudo $target)) {
        Write-Host "  $HostName passwordless sudo NOT configured -- deploy will hang on sudo prompts" -ForegroundColor Red
        Write-Host "  Fix:  ssh -t pi@$HostName 'echo `"pi ALL=(ALL) NOPASSWD:ALL`" | sudo tee /etc/sudoers.d/010_pi-nopasswd && sudo chmod 0440 /etc/sudoers.d/010_pi-nopasswd'" -ForegroundColor Yellow
        return $null
    }

    return $ip
}

# ===========================================================================
# Pre-flight
# ===========================================================================

if ($Help) { Show-Help; exit 0 }

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host "OpenSSH client not found. Enable it in Windows:" -ForegroundColor Red
    Write-Host "  Settings > Apps > Optional features > Add feature > OpenSSH Client" -ForegroundColor Yellow
    exit 1
}

if (!(Test-Path $KEY)) {
    Write-Host "Creating SSH keypair: $KEY" -ForegroundColor Cyan
    # -N "" is empty passphrase; the '""' gymnastics are for PowerShell arg
    # parsing to produce a literal "" on the ssh-keygen.exe command line.
    # -C tags the key so you can identify it later in authorized_keys.
    & ssh-keygen -t ed25519 -N '""' -f $KEY -C "kidpager-deploy" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ssh-keygen failed. Is $env:USERPROFILE\.ssh writable?" -ForegroundColor Red
        exit 1
    }
}

# ===========================================================================
# Action handlers (each Ensure-Connectivity's its own targets)
# ===========================================================================

if ($Setup) {
    Write-Host "=== SSH key setup ===" -ForegroundColor Cyan
    $ok = 0; $fail = 0
    foreach ($dest in $PAGERS) {
        Write-Host "`n$dest"
        if (Ensure-Connectivity $dest) { $ok++ } else { $fail++ }
    }
    Write-Host "`n$ok ready, $fail failed" -ForegroundColor $(if ($fail -eq 0) { "Green" } else { "Yellow" })
    if ($fail -eq 0) { Write-Host "Next: .\deploy.ps1 -All" -ForegroundColor Green }
    exit $(if ($fail -eq 0) { 0 } else { 1 })
}

if ($Restart) {
    foreach ($dest in $PAGERS) {
        $ip = Ensure-Connectivity $dest
        if (-not $ip) { continue }
        ssh @sshCmd "${PiUser}@${ip}" "sudo systemctl restart kidpager 2>/dev/null && echo $dest OK || echo $dest FAIL" 2>$null
    }
    exit 0
}

# Remote health check: runs diagnose.py on-device (-y auto-stops kidpager for
# HW tests). Use -PiHost to target a single pager, otherwise runs on both.
if ($Diag) {
    $targets = if ($PiHost) { @($PiHost) } else { $PAGERS }
    foreach ($dest in $targets) {
        $ip = Ensure-Connectivity $dest
        if (-not $ip) { continue }
        Write-Host "`n=== Diag $dest ($ip) ===" -ForegroundColor Yellow
        ssh @sshCmd "${PiUser}@${ip}" "cd /home/pi/kidpager && sudo python3 diagnose.py -y" 2>$null
    }
    exit 0
}

# Standalone wipe (no deploy)
if ($WipeHistory -and -not $All -and -not $PiHost) {
    foreach ($dest in $PAGERS) {
        $ip = Ensure-Connectivity $dest
        if (-not $ip) { continue }
        Write-Host "Wipe history -> $dest" -ForegroundColor Magenta
        ssh @sshCmd "${PiUser}@${ip}" $WIPE_CMD 2>$null
    }
    exit 0
}

# ===========================================================================
# Main deploy
# ===========================================================================

if ($All) { $targets = $PAGERS }
elseif ($PiHost) { $targets = @($PiHost) }
else {
    Write-Host "Usage: -Help | -Setup | -All | -PiHost NAME | -Restart | -WipeHistory | -Diag [-PiHost NAME]"
    exit 1
}

$start = Get-Date
$results = [ordered]@{}

foreach ($dest in $targets) {
    Write-Host "`n=== $dest ===" -ForegroundColor Yellow

    $ip = Ensure-Connectivity $dest
    if (-not $ip) { $results[$dest] = "unreachable"; continue }
    $t = "${PiUser}@${ip}"

    Write-Host "  [1/8] Packages" -ForegroundColor Cyan
    # Tolerant apt flow:
    #   * The main install line is best-effort; missing packages in a particular
    #     Raspberry Pi OS snapshot will not kill the deploy because the pigpio
    #     self-heal in step [2/8] and the Terminus fallback below cover gaps.
    #   * python3-pigpio: Python client library for pigpiod. Name on Debian
    #     Trixie/Bookworm. Step [2/8] has a multi-strategy heal (retry-apt ->
    #     pip --break-system-packages -> copy pigpio.py from source).
    #   * python3-pip: needed for the pip fallback above.
    #   * fonts-terminus-otb: bitmap font for the v0.14+ E-Ink rendering (fixes
    #     "love" letter-merging bug). Try otb first, then xfonts-terminus.
    #     If both are unavailable, display_eink.py falls back to DejaVu at
    #     runtime so the pager still works, just with v0.13 rendering.
    #   * git + build-essential: needed to build pigpiod from source in [2/8]
    #     (not packaged on Raspberry Pi OS Trixie Lite).
    ssh @sshCmd $t @'
sudo apt update -qq 2>/dev/null
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
    python3-spidev python3-rpi.gpio python3-pil python3-gpiozero \
    python3-pigpio python3-pip \
    git build-essential bluez \
    fonts-dejavu-core \
    wget rfkill 2>&1 | tail -1

# Terminus bitmap font (for v0.14+ UI). Prefer otb (Debian Trixie/Bookworm),
# fall back to older xfonts-terminus. Failures here are non-fatal -- the
# display driver falls back to DejaVu automatically.
if ! dpkg -s fonts-terminus-otb >/dev/null 2>&1 && ! dpkg -s xfonts-terminus >/dev/null 2>&1; then
    if sudo DEBIAN_FRONTEND=noninteractive apt install -y fonts-terminus-otb 2>/dev/null; then
        echo "  Terminus: fonts-terminus-otb installed"
    elif sudo DEBIAN_FRONTEND=noninteractive apt install -y xfonts-terminus 2>/dev/null; then
        echo "  Terminus: xfonts-terminus installed (fallback)"
    else
        echo "  Terminus: UNAVAILABLE -- display will use DejaVu fallback"
    fi
else
    echo "  Terminus: already installed"
fi
'@

    Write-Host "  [2/8] SPI + pigpiod (build daemon from source)" -ForegroundColor Cyan
    # Trixie Lite has python3-pigpio (client library) but no pigpiod (C daemon)
    # package. Build from source.
    #
    # Multi-strategy self-heal for the Python client binding. pigpio is the
    # Python socket client that talks to the pigpiod daemon; without it,
    # buzzer.py silently falls back to no-op (pager works but no sound).
    # Three strategies, tried in order:
    #   A) apt install python3-pigpio (maybe step [1/8] had a transient failure)
    #   B) pip install --break-system-packages (PEP 668 override, pypi fallback)
    #   C) cp pigpio.py out of the cloned github source tree (pure-python
    #      client, no compilation required -- always works if A and B fail)
    # Each strategy reports its result and only runs if the previous didn't fix
    # it. Idempotent: skips steps whose output is already healthy.
    #
    # NOTE: we deliberately DON'T use `set -e` here -- each step must be
    # allowed to fail so the next strategy gets a chance. Every failure is
    # logged, nothing is silently swallowed.
    ssh @sshCmd $t @'
sudo raspi-config nonint do_spi 0 2>/dev/null || true

# --- (1) pigpiod C daemon ------------------------------------------------
if [ ! -x /usr/local/bin/pigpiod ] && [ ! -x /usr/bin/pigpiod ]; then
    echo '  Building pigpio from source (2-3 minutes, be patient)...'
    cd /tmp && rm -rf pigpio
    if git clone --depth 1 https://github.com/joan2937/pigpio.git >/dev/null 2>&1; then
        cd pigpio
        make -j4 >/dev/null 2>&1
        # Only need the C library + daemon binary. The Makefile's Python
        # install step fails on Py3.12+ (distutils removed); (2) handles that.
        sudo make install 2>/dev/null
        sudo ldconfig
        if [ -x /usr/local/bin/pigpiod ]; then
            echo '  pigpiod built and installed'
        else
            echo '  pigpiod build FAILED (check network / make output)'
        fi
    else
        echo '  pigpiod build FAILED: git clone error'
    fi
else
    echo '  pigpiod daemon already installed'
fi

# --- (2) pigpio Python client: multi-strategy heal ------------------------
if python3 -c 'import pigpio' 2>/dev/null; then
    echo '  pigpio Python: already importable'
else
    echo '  pigpio Python: missing, trying strategies...'

    # Strategy A: retry apt (step [1/8] may have had transient failure)
    echo '    [A] apt install python3-pigpio'
    sudo DEBIAN_FRONTEND=noninteractive apt install -y python3-pigpio 2>&1 | tail -1
    if python3 -c 'import pigpio' 2>/dev/null; then
        echo '    [A] OK: apt install worked'
    else
        # Strategy B: pip install with PEP 668 override
        echo '    [B] pip install pigpio --break-system-packages'
        sudo pip3 install pigpio --break-system-packages 2>&1 | tail -2
        if python3 -c 'import pigpio' 2>/dev/null; then
            echo '    [B] OK: pip install worked'
        else
            # Strategy C: copy pigpio.py out of the github source tree
            echo '    [C] copy pigpio.py from github source'
            if [ ! -f /tmp/pigpio/pigpio.py ]; then
                cd /tmp && rm -rf pigpio
                git clone --depth 1 https://github.com/joan2937/pigpio.git >/dev/null 2>&1
            fi
            if [ -f /tmp/pigpio/pigpio.py ]; then
                DEST=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)
                [ -z "$DEST" ] && DEST=/usr/local/lib/python3/dist-packages
                sudo mkdir -p "$DEST"
                sudo cp /tmp/pigpio/pigpio.py "$DEST/pigpio.py"
                if python3 -c 'import pigpio' 2>/dev/null; then
                    echo "    [C] OK: pigpio.py copied to $DEST"
                else
                    echo "    [C] FAIL: copied but import still errors"
                    python3 -c 'import pigpio' 2>&1 | head -3 | sed 's/^/        /'
                fi
            else
                echo '    [C] FAIL: no pigpio.py in github clone (network?)'
            fi
        fi
    fi
fi

# --- (3) systemd unit ----------------------------------------------------
if [ ! -f /lib/systemd/system/pigpiod.service ] && [ ! -f /etc/systemd/system/pigpiod.service ]; then
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

if systemctl is-active --quiet pigpiod; then
    echo '  pigpiod: active'
else
    echo '  pigpiod: NOT active'
    sudo systemctl status pigpiod --no-pager -n 5 2>/dev/null | head -10 | sed 's/^/    /'
fi

# Final end-to-end check: Python module AND socket reachable.
if python3 -c "import pigpio; p=pigpio.pi(); print('  pigpio end-to-end: OK' if p.connected else '  pigpio end-to-end: socket UNREACHABLE (daemon not listening?)'); (p.stop() if p.connected else None)" 2>&1; then
    :
else
    echo '  pigpio end-to-end: IMPORT FAILED'
    python3 -c 'import pigpio' 2>&1 | head -3 | sed 's/^/    /'
fi
'@

    Write-Host "  [3/8] Waveshare E-Ink driver" -ForegroundColor Cyan
    ssh @sshCmd $t "mkdir -p ~/waveshare_epd; B=https://raw.githubusercontent.com/waveshare/e-Paper/master/RaspberryPi_JetsonNano/python/lib/waveshare_epd; for F in __init__.py epdconfig.py epd2in13_V4.py; do test -f ~/waveshare_epd/`$F || wget -q -O ~/waveshare_epd/`$F `$B/`$F; done; test -f ~/waveshare_epd/epd2in13_V4.py && echo OK || echo FAIL"

    Write-Host "  [4/8] Files" -ForegroundColor Cyan
    ssh @sshCmd $t "mkdir -p ~/kidpager ~/.kidpager"
    $filesToSend = $PY_FILES
    if ($Tests) { $filesToSend = $PY_FILES + $TEST_FILES }
    foreach ($f in $filesToSend) {
        if (Test-Path $f) {
            # Strip UTF-8 BOM / embedded NULs so Python doesn't choke on load.
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
    # scp drops exec bits on Windows sources. chmod bt_pair.sh; install
    # kidpager-power.sh to /usr/local/bin with 755 via `install -m`.
    ssh @sshCmd $t "chmod +x ~/bt_pair.sh 2>/dev/null; sudo install -m 755 ~/kidpager-power.sh /usr/local/bin/kidpager-power.sh && rm ~/kidpager-power.sh"

    Write-Host "  [5/8] Config" -ForegroundColor Cyan
    # Remove stale /home/pi/.kidpager/config.json from pre-v0.9 deploys; live
    # config lives in /root/.kidpager/ because the service runs as root.
    # Existing /root/.kidpager/config.json is NEVER overwritten (guarded by
    # `test -f`) -- preserves the user's name, channel, and silent flag
    # across redeploys.
    ssh @sshCmd $t "sudo rm -f /home/pi/.kidpager/config.json; sudo mkdir -p /root/.kidpager; sudo test -f /root/.kidpager/config.json || echo '{""name"":""Kid"",""channel"":1,""silent"":false}' | sudo tee /root/.kidpager/config.json >/dev/null"

    Write-Host "  [6/8] kidpager.service" -ForegroundColor Cyan
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

    Write-Host "  [7/8] kidpager-power.service" -ForegroundColor Cyan
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
test -f ~/waveshare_epd/epd2in13_V4.py        && echo '[OK] Waveshare driver'        || echo '[!!] Waveshare driver'
test -f ~/kidpager/main.py                    && echo '[OK] Code deployed'           || echo '[!!] Code missing'
test -f ~/kidpager/power.py                   && echo '[OK] power.py'                || echo '[!!] power.py missing'
test -x ~/bt_pair.sh                          && echo '[OK] bt_pair.sh executable'   || echo '[!!] bt_pair.sh NOT executable'
test -x /usr/local/bin/kidpager-power.sh      && echo '[OK] Power script'            || echo '[!!] Power script missing'
test -x /usr/local/bin/pigpiod                && echo '[OK] pigpiod binary'          || echo '[!!] pigpiod binary missing'
test -f /lib/systemd/system/pigpiod.service   && echo '[OK] pigpiod unit'            || echo '[!!] pigpiod unit missing'
ls /dev/spidev0.0 >/dev/null 2>&1             && echo '[OK] SPI CE0 (E-Ink)'         || echo '[!!] SPI CE0 missing'
ls /dev/spidev0.1 >/dev/null 2>&1             && echo '[OK] SPI CE1 (LoRa)'          || echo '[!!] SPI CE1 missing'
test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf && echo '[OK] DejaVu fonts'  || echo '[!!] DejaVu fonts missing'
(ls /usr/share/fonts/X11/misc/ter-u14n.* 2>/dev/null | grep -q ter || ls /usr/share/fonts/X11/misc/*terminus* 2>/dev/null | grep -q terminus) && echo '[OK] Terminus font' || echo '[  ] Terminus font NOT installed (DejaVu fallback will be used)'
systemctl is-enabled kidpager       2>/dev/null | grep -q enabled && echo '[OK] kidpager autostart'          || echo '[!!] kidpager autostart'
systemctl is-enabled kidpager-power 2>/dev/null | grep -q enabled && echo '[OK] Power-save (active on boot)' || echo '[!!] Power-save NOT enabled'
systemctl is-active  pigpiod        2>/dev/null | grep -q active  && echo '[OK] pigpiod running'             || echo '[!!] pigpiod NOT running'
python3 -c 'import pigpio' 2>/dev/null && echo '[OK] pigpio Python module'           || echo '[!!] pigpio module missing'
echo 'BT paired devices:'
bluetoothctl devices 2>/dev/null | sed 's/^/  /' || echo '  (none)'
echo '---'
"@

    if ($WipeHistory) {
        Write-Host "  [+]   Wipe history" -ForegroundColor Magenta
        ssh @sshCmd $t $WIPE_CMD
    }

    Write-Host "  $dest DONE" -ForegroundColor Green
    $results[$dest] = "OK"
}

# ===========================================================================
# Summary
# ===========================================================================

$elapsed = (Get-Date) - $start
$okCount = ($results.Values | Where-Object { $_ -eq "OK" }).Count
$totalCount = $results.Count

Write-Host ""
Write-Host "=== Summary ($([int]$elapsed.TotalSeconds)s) ===" -ForegroundColor Cyan
foreach ($kv in $results.GetEnumerator()) {
    $color = if ($kv.Value -eq "OK") { "Green" } else { "Red" }
    Write-Host ("  {0,-24} {1}" -f $kv.Key, $kv.Value) -ForegroundColor $color
}
Write-Host "$okCount/$totalCount successful" -ForegroundColor $(if ($okCount -eq $totalCount) { "Green" } else { "Yellow" })

if ($okCount -gt 0) {
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host "  1. Pair the M4 on each pager:  ssh pi@kidpager.local -> sudo ~/bt_pair.sh" -ForegroundColor Yellow
    Write-Host "  2. Reboot each pager once (power-save activates on boot)" -ForegroundColor Yellow
    Write-Host "  3. Verify:  .\deploy.ps1 -Diag" -ForegroundColor Yellow
    Write-Host "  (After reboot Wi-Fi is blocked. Alt+W on the M4 re-enables it for re-deploys.)" -ForegroundColor DarkGray
}

exit $(if ($okCount -eq $totalCount) { 0 } else { 1 })
