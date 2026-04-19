# BT Pair — Usage: .\bt.ps1 -PiHost kidpager.local
param([string]$PiHost = "kidpager.local", [string]$PiUser = "pi")
$KEY = "$env:USERPROFILE\.ssh\id_kidpager"
$ssh = @("-F", "nul", "-i", $KEY, "-o", "StrictHostKeyChecking=no", "-t")
$scp = @("-F", "nul", "-i", $KEY, "-o", "StrictHostKeyChecking=no")
$t = "${PiUser}@${PiHost}"
Write-Host "=== BT Pair on $PiHost ===" -ForegroundColor Yellow
$clean = (Get-Content bt_pair.sh -Raw) -replace "`r", ""
[System.IO.File]::WriteAllText("$env:TEMP\bt_pair.sh", $clean, [System.Text.UTF8Encoding]::new($false))
scp @scp "$env:TEMP\bt_pair.sh" "${t}:~/bt_pair.sh" 2>$null
Remove-Item "$env:TEMP\bt_pair.sh"
ssh @ssh $t "sudo bash ~/bt_pair.sh"
