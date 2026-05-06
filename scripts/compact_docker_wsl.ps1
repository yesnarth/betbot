# Compact-Docker-WSL — reclaim disk space held by Docker Desktop on Windows.
#
# Why: WSL2 grows its virtual disk as Docker pulls images and builds layers,
# but it NEVER shrinks back to the host filesystem — even after
# `docker system prune`. The .vhdx file stays large until you compact it.
#
# This script:
#   1. Stops BetBot (and any other compose stacks in the current dir)
#   2. Quits Docker Desktop
#   3. Shuts down WSL (releases the vhdx lock)
#   4. Compacts the docker_data.vhdx using diskpart
#   5. Restarts Docker Desktop
#
# Run as the owner of Docker Desktop (no admin needed). Takes 2-5 minutes.
#
# Usage:
#   pwsh -ExecutionPolicy Bypass -File scripts\compact_docker_wsl.ps1

param(
    [string]$VhdxPath = "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx",
    [switch]$SkipRestart
)

$ErrorActionPreference = "Continue"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

if (-not (Test-Path $VhdxPath)) {
    Write-Error "vhdx not found at $VhdxPath. Maybe Docker Desktop uses Hyper-V backend?"
    exit 1
}

$beforeMb = [math]::Round((Get-Item $VhdxPath).Length / 1MB, 0)
Write-Host "Current docker_data.vhdx size: $beforeMb MB ($([math]::Round($beforeMb/1024, 2)) GB)" -ForegroundColor Yellow

Write-Step "1/5 Stopping Docker Compose stacks (best effort)"
docker compose down 2>$null | Out-Null

Write-Step "2/5 Quitting Docker Desktop"
Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process -Name "com.docker.backend" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 5

Write-Step "3/5 wsl --shutdown"
wsl --shutdown
Start-Sleep -Seconds 3

Write-Step "4/5 Compacting $VhdxPath (this is the slow step)"
$diskpartScript = @"
select vdisk file="$VhdxPath"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@
$tmp = [System.IO.Path]::GetTempFileName()
$diskpartScript | Out-File -FilePath $tmp -Encoding ascii
diskpart /s $tmp
Remove-Item $tmp -ErrorAction SilentlyContinue

$afterMb = [math]::Round((Get-Item $VhdxPath).Length / 1MB, 0)
$reclaimedMb = $beforeMb - $afterMb
Write-Host ""
Write-Host ("Reclaimed: {0} MB ({1} GB)" -f $reclaimedMb, [math]::Round($reclaimedMb/1024, 2)) -ForegroundColor Green
Write-Host ("New vhdx size: {0} MB ({1} GB)"   -f $afterMb,    [math]::Round($afterMb/1024, 2))    -ForegroundColor Green

if (-not $SkipRestart) {
    Write-Step "5/5 Restarting Docker Desktop"
    $dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerExe) {
        Start-Process $dockerExe
        Write-Host "Docker Desktop relaunched. Wait ~30s then run: docker compose up -d"
    } else {
        Write-Warning "Docker Desktop.exe not found at default path — start it manually."
    }
} else {
    Write-Host "Skipping Docker Desktop restart (-SkipRestart)." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
