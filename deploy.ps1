# KidPager Deploy
# Setup:        .\deploy.ps1 -Setup
# Deploy:       .\deploy.ps1 -All
# One:          .\deploy.ps1 -PiHost kidpager.local
# Restart:      .\deploy.ps1 -Restart
# Wipe chat:    .\deploy.ps1 -WipeHistory           (wipe only, both pagers)
# Deploy+wipe:  .\deploy.ps1 -All -WipeHistory      (deploy then wipe)
# Deploy+tests: .\deploy.ps1 -All -Tests            (also copies test_*.py)
# Diagnose:     .\deploy.ps1 -Diag                  (run full diagnose.py on both)
# Diagnose one: .\deploy.ps1 -Diag -PiHost kidpager.local

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
# manual ssh-keygen -R. We're on a LAN and authenticating by key, so MITM risk
# is equivalent to the regular StrictHostKeyChecking=no case we were using anyway.
$sshCmd = @(
    "-F", "nul",
    "-i", $KEY,
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR"
)
# Production code + diagnose.py (diagnose is useful in the field).
$PY_FILES = @("pins.py","lora.py","display_eink.py","config.py","keyboard.py","buzzer.py","ui.py","main.py","power.py","diagnose.py")
# Developer-only smoke tests; only copied when -Tests is passed.
$TEST_FILES = @("test_lora_spi.py","test_buzzer.py","test_power.py")

# Service runs as User=root, so ~ expands to /root - history lives there.
# The /home/pi path is cleared too just to be thorough.
$WIPE_CMD = "sudo systemctl stop kidpager 2>/dev/null; sudo rm -f /root/.kidpager/history.json /home/pi/.kidpager/history.json; sudo systemctl start kidpager 2>/dev/null; echo wiped"

if ($Setup) {
    if (!(Test-Path $KEY)) { ssh-keygen -t ed25519 -N '""' -f $KEY }
    foreach ($dest in $PAGERS) {
        Write-Host "Key -> $dest (password once)..." -ForegroundColor Cyan
        Get-Content "${KEY}.pub" | ssh -F nul -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "${PiUser}@${dest}" "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys" 2>$null
    }
    Write-Host "Done! Run: .\deploy.ps1 -All" -ForegroundColor Green; exit 0
}

if ($Restart) {
    foreach ($dest in $PAGERS) {
        ssh @sshCmd "${PiUser}@${dest}" "sudo systemctl restart kidpager 2>/dev/null && echo $dest OK || echo $dest FAIL" 2>$null
    }
    exit 0
}

# Remote health check: runs diagnose.py on-device (with -y to auto-stop kidpager
# for HW tests). Use -PiHost to target a single pager, otherwise runs on both.
if ($Diag) {
    $targets = if ($PiHost) { @($PiHost) } else { $PAGERS }
    foreach ($dest in $targets) {
        Write-Host "`n=== Diag $dest ===" -ForegroundColor Yellow
        ssh @sshCmd "${PiUser}@${dest}" "cd /home/pi/kidpager && sudo python3 diagnose.py -y" 2>$null
    }
    exit 0
}

# Standalone wipe (no deploy)
if ($WipeHistory -and -not $All -and -not $PiHost) {
    foreach ($dest in $PAGERS) {
        Write-Host "Wipe history -> $dest" -ForegroundColor Magenta
        ssh @sshCmd "${PiUser}@${dest}" $WIPE_CMD 2>$null
    }
    exit 0
}

if ($All) { $targets = $PAGERS }
elseif ($PiHost) { $targets = @($PiHost) }
else { Write-Host "Usage: -Setup | -All | -PiHost name | -Restart | -WipeHistory | -Diag [-PiHost name]"; exit 1 }

if (!(Test-Path $KEY)) { Write-Host "Run -Setup first" -ForegroundColor Red; exit 1 }

foreach ($dest in $targets) {
    $t = "${PiUser}@${dest}"
    Write-Host "`n=== $dest ===" -ForegroundColor Yellow

    $ok = ssh @sshCmd $t "echo ok" 2>$null
    if ($ok -ne "ok") { Write-Host "  UNREACHABLE" -ForegroundColor Red; continue }

    Write-Host "  [1/8] Packages" -ForegroundColor Cyan
    ssh @sshCmd $t "sudo apt update -qq 2>/dev/null; sudo apt install -y python3-spidev python3-rpi.gpio python3-pil python3-gpiozero python3-pigpio bluez fonts-dejavu-core wget rfkill 2>/dev/null | tail -1"

    Write-Host "  [2/8] SPI + pigpiod" -ForegroundColor Cyan
    # Trixie has python3-pigpio in apt (installed in [1/8]). Just enable SPI,
    # start pigpiod, and verify. Previously we built pigpio from source for
    # Bookworm (where it was dropped from the repos); no longer needed.
    ssh @sshCmd $t @"
sudo raspi-config nonint do_spi 0 2>/dev/null
sudo systemctl enable pigpiod --now 2>/dev/null
systemctl is-active pigpiod 2>/dev/null | grep -q active && echo '  pigpiod: active' || echo '  pigpiod: FAILED'
python3 -c 'import pigpio' 2>/dev/null && echo '  pigpio module: OK' || echo '  pigpio module: FAILED'
"@

    Write-Host "  [3/8] Waveshare" -ForegroundColor Cyan
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
    # Also copy bt_pair.sh (BT pairing) and kidpager-power.sh (boot-time power config)
    foreach ($sh in @("bt_pair.sh", "kidpager-power.sh")) {
        if (Test-Path $sh) {
            $clean = (Get-Content $sh -Raw) -replace "`r", ""
            [System.IO.File]::WriteAllText("$env:TEMP\$sh", $clean, [System.Text.UTF8Encoding]::new($false))
            scp @sshCmd -q "$env:TEMP\$sh" "${t}:~/${sh}" 2>$null
            Remove-Item "$env:TEMP\$sh"
        }
    }
    # kidpager-power.sh must live in /usr/local/bin/ so the systemd unit can ExecStart it.
    ssh @sshCmd $t "sudo install -m 755 ~/kidpager-power.sh /usr/local/bin/kidpager-power.sh && rm ~/kidpager-power.sh"

    Write-Host "  [5/8] Config" -ForegroundColor Cyan
    # Remove stale /home/pi/.kidpager/config.json from pre-v0.9 deploys; live
    # config lives in /root/.kidpager/ because the service runs as root.
    ssh @sshCmd $t "sudo rm -f /home/pi/.kidpager/config.json; sudo mkdir -p /root/.kidpager; sudo test -f /root/.kidpager/config.json || echo '{""name"":""Kid"",""channel"":1}' | sudo tee /root/.kidpager/config.json >/dev/null"

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
    # Runs /usr/local/bin/kidpager-power.sh once at boot: rfkill wifi, powersave
    # governor, ACT LED off. Listed as Before=kidpager.service so the main pager
    # starts in the already-saved state.
    #
    # IMPORTANT: enable WITHOUT --now. The oneshot does 'rfkill block wifi', which
    # severs the SSH connection we're deploying over. Power-save activates on the
    # next boot; this is harmless because the Pi will reboot at the end of field
    # setup anyway.
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
    # Note: Wi-Fi-blocked check is intentionally omitted - it would always show
    # FAIL until first reboot (see step 7/8 comment).
    ssh @sshCmd $t "echo '---'; test -f ~/waveshare_epd/epd2in13_V4.py && echo '[OK] Waveshare' || echo '[!!] Waveshare'; test -f ~/kidpager/main.py && echo '[OK] Code' || echo '[!!] Code'; test -x /usr/local/bin/kidpager-power.sh && echo '[OK] Power script' || echo '[!!] Power script'; ls /dev/spidev0.0 >/dev/null 2>&1 && echo '[OK] SPI' || echo '[!!] SPI'; test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf && echo '[OK] Fonts' || echo '[!!] Fonts'; systemctl is-enabled kidpager 2>/dev/null | grep -q enabled && echo '[OK] Autostart' || echo '[!!] Autostart'; systemctl is-enabled kidpager-power 2>/dev/null | grep -q enabled && echo '[OK] Power-save enabled (active after reboot)' || echo '[!!] Power-save not enabled'; systemctl is-active pigpiod 2>/dev/null | grep -q active && echo '[OK] pigpiod' || echo '[!!] pigpiod'; python3 -c 'import pigpio' 2>/dev/null && echo '[OK] pigpio module' || echo '[!!] pigpio module'; echo 'BT:'; bluetoothctl devices 2>/dev/null; echo '---'"

    if ($WipeHistory) {
        Write-Host "  [+]   Wipe history" -ForegroundColor Magenta
        ssh @sshCmd $t $WIPE_CMD
    }

    Write-Host "  $dest DONE" -ForegroundColor Green
}

Write-Host "`n=== Complete ===" -ForegroundColor Green
Write-Host "Power-save activates on next reboot." -ForegroundColor Yellow
Write-Host "After reboot, Wi-Fi is blocked. Alt+W on the M4 re-enables it for redeploys." -ForegroundColor Yellow