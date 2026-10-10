<#
  Caps the WSL2 VM that Docker Desktop runs in, so chaos experiments can never starve Windows.
  Run in PowerShell:   powershell -ExecutionPolicy Bypass -File scripts\setup-wslconfig.ps1
  Optional:            ... -MemoryGB 6 -Processors 4 -SwapGB 2

  Default is 4 GB, not the 6 GB in the M1 task file: the host has ~5.2 GB free, and a 6 GB cap
  lets the VM's page cache grow past free RAM, so Windows starts paging and every latency
  measurement becomes noise. Our stack needs ~2.5-3 GB (sandbox ~2 GB + controller ~0.4 GB).
#>
param(
    [int]$MemoryGB = 4,
    [int]$Processors = 4,
    [int]$SwapGB = 2
)

$path = Join-Path $env:USERPROFILE ".wslconfig"

if (Test-Path $path) {
    $backup = "$path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $path $backup
    Write-Host "Existing .wslconfig backed up to $backup"
}

@"
[wsl2]
memory=${MemoryGB}GB
processors=$Processors
swap=${SwapGB}GB

[experimental]
autoMemoryReclaim=gradual
"@ | Set-Content -Path $path -Encoding ASCII

Write-Host "Wrote $path :"
Get-Content $path

Write-Host "`nShutting down WSL (Docker Desktop will stop)..."
wsl --shutdown

Write-Host "Done. Start Docker Desktop again, wait for 'Engine running', then run: docker info --format '{{.MemTotal}}'"
Write-Host "Expected MemTotal ~ $MemoryGB GB."
