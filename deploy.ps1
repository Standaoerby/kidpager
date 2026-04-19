# KidPager Deploy
# Setup:   .\deploy.ps1 -Setup
# Deploy:  .\deploy.ps1 -All
# One:     .\deploy.ps1 -PiHost kidpager.local
# Restart: .\deploy.ps1 -Restart

param(
    [string]$PiHost = "",
    [string]$PiUser = "pi",
    [switch]$All,
    [switch]$Setup,
    [switch]$Restart
)

$PAGERS = @("kidpager.local", "kidpager2.local")
$KEY = "$env:USERPROFILE\.ssh\id_kidpager"
$sshCmd = @("-F", "nul", "-i", $KEY, "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no")
$PY_FILES = @("pins.py","lora.py","display_eink.py","config.py","keyboard.py","buzzer.py","ui.py","main.py","test_lora_spi.py")

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

if ($All) { $targets = $PAGERS }
elseif ($PiHost) { $targets = @($PiHost) }
else { Write-Host "Usage: -Setup | -All | -PiHost name | -Restart"; exit 1 }

if (!(Test-Path $KEY)) { Write-Host "Run -Setup first" -ForegroundColor Red; exit 1 }

foreach ($dest in $targets) {
    $t = "${PiUser}@${dest}"
    Write-Host "`n=== $dest ===" -ForegroundColor Yellow

    $ok = ssh @sshCmd $t "echo ok" 2>$null
    if ($ok -ne "ok") { Write-Host "  UNREACHABLE" -ForegroundColor Red; continue }

    Write-Host "  [1/7] Packages" -ForegroundColor Cyan
    ssh @sshCmd $t "sudo apt update -qq 2>/dev/null; sudo apt install -y python3-spidev python3-rpi.gpio python3-pil python3-gpiozero python3-pigpio pigpio bluez fonts-dejavu-core wget 2>/dev/null | tail -1"

    Write-Host "  [2/7] SPI + pigpiod" -ForegroundColor Cyan
    ssh @sshCmd $t "sudo raspi-config nonint do_spi 0 2>/dev/null; sudo systemctl enable pigpiod --now 2>/dev/null; echo done"

    Write-Host "  [3/7] Waveshare" -ForegroundColor Cyan
    ssh @sshCmd $t "mkdir -p ~/waveshare_epd; B=https://raw.githubusercontent.com/waveshare/e-Paper/master/RaspberryPi_JetsonNano/python/lib/waveshare_epd; for F in __init__.py epdconfig.py epd2in13_V4.py; do test -f ~/waveshare_epd/`$F || wget -q -O ~/waveshare_epd/`$F `$B/`$F; done; test -f ~/waveshare_epd/epd2in13_V4.py && echo OK || echo FAIL"

    Write-Host "  [4/7] Files" -ForegroundColor Cyan
    ssh @sshCmd $t "mkdir -p ~/kidpager ~/.kidpager"
    foreach ($f in $PY_FILES) {
        if (Test-Path $f) {
            $bytes = [System.IO.File]::ReadAllBytes($f)
            $clean = $bytes | Where-Object { $_ -ne 0 }
            $tmp = [System.IO.Path]::GetTempFileName()
            [System.IO.File]::WriteAllBytes($tmp, [byte[]]$clean)
            scp @sshCmd -q $tmp "${t}:~/kidpager/${f}" 2>$null
            Remove-Item $tmp
        }
    }
    # Also copy bt_pair.sh
    if (Test-Path "bt_pair.sh") {
        $clean = (Get-Content "bt_pair.sh" -Raw) -replace "`r", ""
        [System.IO.File]::WriteAllText("$env:TEMP\bt_pair.sh", $clean, [System.Text.UTF8Encoding]::new($false))
        scp @sshCmd -q "$env:TEMP\bt_pair.sh" "${t}:~/bt_pair.sh" 2>$null
        Remove-Item "$env:TEMP\bt_pair.sh"
    }

    Write-Host "  [5/7] Config" -ForegroundColor Cyan
    ssh @sshCmd $t "test -f ~/.kidpager/config.json || echo '{""name"":""Kid"",""channel"":1}' > ~/.kidpager/config.json"

    Write-Host "  [6/7] Service" -ForegroundColor Cyan
    ssh @sshCmd $t "sudo bash -c 'cat > /etc/systemd/system/kidpager.service << SVCEOF
[Unit]
Description=KidPager
After=multi-user.target bluetooth.target pigpiod.service
Wants=bluetooth.target pigpiod.service
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

    Write-Host "  [7/7] Verify" -ForegroundColor Cyan
    ssh @sshCmd $t "echo '---'; test -f ~/waveshare_epd/epd2in13_V4.py && echo '[OK] Waveshare' || echo '[!!] Waveshare'; test -f ~/kidpager/main.py && echo '[OK] Code' || echo '[!!] Code'; ls /dev/spidev0.0 >/dev/null 2>&1 && echo '[OK] SPI' || echo '[!!] SPI'; test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf && echo '[OK] Fonts' || echo '[!!] Fonts'; systemctl is-enabled kidpager 2>/dev/null | grep -q enabled && echo '[OK] Autostart' || echo '[!!] Autostart'; systemctl is-active pigpiod 2>/dev/null | grep -q active && echo '[OK] pigpiod' || echo '[!!] pigpiod'; echo 'BT:'; bluetoothctl devices 2>/dev/null; echo '---'"

    Write-Host "  $dest DONE" -ForegroundColor Green
}

Write-Host "`n=== Complete ===" -ForegroundColor Green
