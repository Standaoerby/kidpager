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
$sshCmd = @("-F", "nul", "-i", $KEY, "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no")
# Production code + diagnose.py (diagnose is useful in the field).
$PY_FILES = @("pins.py","lora.py","display_eink.py","config.py","keyboard.py","buzzer.py","ui.py","main.py","power.py","diagnose.py")
# Developer-only smoke tests; only copied when -Tests is passed.
$TEST_FILES = @("test_lora_spi.py","test_buzzer.py","test_power.py")

# Service runs as User=root, so ~ expands to /root — history lives there.
# The /home/pi path is cleared too just to be thorough.
$WIPE_CMD = "sudo systemctl stop kidpager 2>/dev/null; sudo rm -f /root/.kidpager/history.json /home/pi/.kidpager/history.json; sudo systemctl start kidpager 2>/dev/null; echo wiped"

if ($Setup) {
    if (!(Test-Path $KEY)) { ssh-keygen -t ed25519 -N '""' -f $KEY }
    foreach ($dest in $PAGERS) {
        Write-Host "Key -> $dest (password once)..." -ForegroundColor Cyan
        type "${KEY}.pub" | ssh -F nul -o StrictHostKeyChecking=no "${PiUser}@${dest}" "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys" 2>$null
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
    ssh @sshCmd $t "sudo apt update -qq 2>/dev/null; sudo apt install -y python3-spidev python3-rpi.gpio python3-pil python3-gpiozero python3-pip git build-essential bluez fonts-dejavu-core wget rfkill 2>/dev/null | tail -1"

    Write-Host "  [2/8] SPI + pigpio (build from source on Bookworm)" -ForegroundColor Cyan
    # pigpio was removed from Raspberry Pi OS Bookworm repos (doesn't support RP1 on Pi 5).
    # For Pi Zero/2/3/4 it still works: build C library from source, install Python module
    # via pip (setup.py uses removed distutils), and drop in the systemd unit manually.
    ssh @sshCmd $t @"
sudo raspi-config nonint do_spi 0 2>/dev/null

# Build and install the C library/daemon if missing
if [ ! -x /usr/local/bin/pigpiod ]; then
    echo '  Building pigpio from source...'
    cd /tmp && rm -rf pigpio
    git clone --depth 1 https://github.com/joan2937/pigpio.git >/dev/null 2>&1
    cd pigpio && make -j4 >/dev/null 2>&1
    # make install Python step fails on Py3.12 (distutils removed). Ignore that;
    # the binary/lib install steps before it still succeed.
    sudo make install 2>/dev/null || true
    # Refresh dynamic linker cache so pigpiod can find libpigpio.so.1 in /usr/local/lib
    sudo ldconfig
    # Drop in the systemd unit manually (make install exits before it otherwise)
    if [ ! -f /lib/systemd/system/pigpiod.service ]; then
        sudo cp /tmp/pigpio/util/pigpiod.service /lib/systemd/system/ 2>/dev/null || \
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
    fi
    echo '  pigpio C library installed'
else
    echo '  pigpio C library already present'
fi

# Python module — via pip, since setup.py is broken on Py3.12.
# NOTE: the pip wheel drops its own pigpiod.service at /usr/lib/systemd/system/
# that expects the binary at /usr/bin/pigpiod. Our source build put it in
# /usr/local/bin/, so we symlink to keep whichever unit file wins happy.
if ! python3 -c 'import pigpio' 2>/dev/null; then
    sudo pip3 install pigpio --break-system-packages >/dev/null 2>&1
    echo '  pigpio Python module installed via pip'
else
    echo '  pigpio Python module already installed'
fi

if [ -x /usr/local/bin/pigpiod ] && [ ! -e /usr/bin/pigpiod ]; then
    sudo ln -s /usr/local/bin/pigpiod /usr/bin/pigpiod
    echo '  symlinked /usr/bin/pigpiod -> /usr/local/bin/pigpiod'
fi

sudo systemctl daemon-reload
sudo systemctl enable pigpiod --now 2>/dev/null
systemctl is-active pigpiod 2>/dev/null | grep -q active && echo '  pigpiod: active' || echo '  pigpiod: FAILED'
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
    ssh @sshCmd $t "test -f ~/.kidpager/config.json || echo '{""name"":""Kid"",""channel"":1}' > ~/.kidpager/config.json"

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
systemctl daemon-reload && systemctl enable --now kidpager-power && echo OK'"

    Write-Host "  [8/8] Verify" -ForegroundColor Cyan
    ssh @sshCmd $t "echo '---'; test -f ~/waveshare_epd/epd2in13_V4.py && echo '[OK] Waveshare' || echo '[!!] Waveshare'; test -f ~/kidpager/main.py && echo '[OK] Code' || echo '[!!] Code'; test -x /usr/local/bin/kidpager-power.sh && echo '[OK] Power script' || echo '[!!] Power script'; ls /dev/spidev0.0 >/dev/null 2>&1 && echo '[OK] SPI' || echo '[!!] SPI'; test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf && echo '[OK] Fonts' || echo '[!!] Fonts'; systemctl is-enabled kidpager 2>/dev/null | grep -q enabled && echo '[OK] Autostart' || echo '[!!] Autostart'; systemctl is-active kidpager-power 2>/dev/null | grep -q active && echo '[OK] Power-save' || echo '[!!] Power-save'; systemctl is-active pigpiod 2>/dev/null | grep -q active && echo '[OK] pigpiod' || echo '[!!] pigpiod'; python3 -c 'import pigpio' 2>/dev/null && echo '[OK] pigpio module' || echo '[!!] pigpio module'; rfkill list wifi 2>/dev/null | grep -q 'Soft blocked: yes' && echo '[OK] Wi-Fi blocked' || echo '[!!] Wi-Fi NOT blocked'; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null | grep -q powersave && echo '[OK] CPU powersave' || echo '[!!] CPU governor'; echo 'BT:'; bluetoothctl devices 2>/dev/null; echo '---'"

    if ($WipeHistory) {
        Write-Host "  [+]   Wipe history" -ForegroundColor Magenta
        ssh @sshCmd $t $WIPE_CMD
    }

    Write-Host "  $dest DONE" -ForegroundColor Green
}

Write-Host "`n=== Complete ===" -ForegroundColor Green
